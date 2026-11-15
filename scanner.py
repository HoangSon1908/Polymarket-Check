import time
from datetime import datetime, timezone
import requests
import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="Polymarket Copy Signal Scanner",
    page_icon="⚡",
    layout="wide",
)

POSITIONS_URL = "https://data-api.polymarket.com/v2/positions"
DEFAULT_WALLET = "0xc9bfc7986767173b4f1c3fc9b2c6b255fc7381c0"


def fetch_positions_v2(wallet: str, status_val: str) -> list[dict]:
    """Kéo vị thế theo chuẩn Data API v2 (lọc status + phân trang cursor)."""
    rows = []
    params = {
        "user": wallet.strip(),
        "status": status_val,
        "limit": 500,
    }

    for _ in range(50):
        res = requests.get(POSITIONS_URL, params=params, timeout=15)
        if res.status_code == 429:
            time.sleep(2.0)
            continue
        res.raise_for_status()
        payload = res.json()

        batch = payload.get("data") or []
        rows.extend(batch)

        pagination = payload.get("pagination") or {}
        next_cursor = pagination.get("next_cursor")
        has_more = pagination.get("has_more", False)

        if not next_cursor or not has_more:
            break

        # API v2 yêu cầu giữ nguyên anchor user & status khi dùng cursor, không truyền offset
        params = {
            "user": wallet.strip(),
            "status": status_val,
            "cursor": next_cursor,
        }

    return rows


def format_time_ago(epoch_val) -> str:
    if pd.isna(epoch_val) or not epoch_val:
        return "N/A"
    try:
        dt = datetime.fromtimestamp(float(epoch_val), tz=timezone.utc)
        diff_min = (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
        if diff_min < 0:
            return "Vừa xong"
        if diff_min < 60:
            return f"{int(diff_min)} phút trước"
        if diff_min < 1440:
            return f"{diff_min / 60:.1f} giờ trước"
        return f"{diff_min / 1440:.1f} ngày trước"
    except Exception:
        return "N/A"


def evaluate_positions(
    raw_open: list[dict],
    multiplier: float,
    min_user_bet: float,
    max_user_cap: float,
    max_slippage_cents: float,
    block_50_60: bool,
    max_fav_prob: float,
) -> pd.DataFrame:
    if not raw_open:
        return pd.DataFrame()

    df = pd.DataFrame(raw_open)

    def get_col(candidates: list[str], default=None) -> pd.Series:
        for c in candidates:
            if c in df.columns:
                return df[c]
        return pd.Series([default] * len(df), index=df.index)

    df["title"] = get_col(["title", "question"], default="N/A").astype(str)
    df["outcome"] = get_col(["outcome"], default="").astype(str)
    df["avg_price"] = pd.to_numeric(get_col(["avg_price"]), errors="coerce").fillna(0.0)
    df["entry_cost_usdc"] = pd.to_numeric(get_col(["entry_cost_usdc"]), errors="coerce").fillna(0.0)
    df["current_size"] = pd.to_numeric(get_col(["current_size"]), errors="coerce").fillna(0.0)
    df["current_value"] = pd.to_numeric(get_col(["current_value"]), errors="coerce").fillna(0.0)
    df["unrealized_pnl"] = pd.to_numeric(get_col(["unrealized_pnl"]), errors="coerce").fillna(0.0)

    # Giá thị trường hiện tại (ưu tiên current_price, fallback current_value / current_size)
    raw_cur_p = pd.to_numeric(get_col(["current_price"]), errors="coerce")
    implied_cur_p = np.where(df["current_size"] > 0, df["current_value"] / df["current_size"], df["avg_price"])
    df["cur_price"] = raw_cur_p.where(raw_cur_p.notna() & (raw_cur_p > 0), implied_cur_p).fillna(df["avg_price"])

    # Quy tắc 1 & 3: Tính số tiền vào lệnh (không làm tròn lên, khóa trần max_user_cap)
    df["raw_user_bet"] = df["entry_cost_usdc"] * multiplier
    df["final_user_bet"] = df["raw_user_bet"].clip(upper=max_user_cap).round(2)

    # Quy tắc 4: Tính độ trượt giá hiện tại & giá mua trần tối đa cho phép
    max_slip_dec = max_slippage_cents / 100.0
    df["slippage_cents"] = (df["cur_price"] - df["avg_price"]) * 100.0
    df["max_limit_cents"] = ((df["avg_price"] + max_slip_dec).clip(upper=0.99)) * 100.0

    last_ts = pd.to_numeric(get_col(["last_event_at", "updated_at", "timestamp"]), errors="coerce")
    df["last_buy_ago"] = last_ts.apply(format_time_ago)

    slug_col = get_col(["event_slug", "slug"], default="")
    df["url"] = slug_col.apply(
        lambda s: f"https://polymarket.com/event/{s}" if pd.notna(s) and str(s).strip() else ""
    )

    # Phân loại từng kèo theo Bộ 5 Quy Tắc
    statuses = []
    reasons = []

    for _, r in df.iterrows():
        p = r["avg_price"]
        cur_p = r["cur_price"]
        raw_bet = r["raw_user_bet"]
        slip_c = r["slippage_cents"]

        # Kiểm tra trận đã gần ngã ngũ / live sâu (giá về sát 0 hoặc 1)
        if cur_p <= 0.02 or cur_p >= 0.98:
            statuses.append("REJECTED")
            reasons.append(f"Kèo đã ngã ngũ/sát giờ đóng (Giá {cur_p*100:.1f}¢)")
            continue

        # Quy tắc 1: Ngưỡng kích hoạt tối thiểu (>= $1.00, không làm tròn lên)
        if raw_bet < min_user_bet:
            min_whale_req = min_user_bet / multiplier if multiplier > 0 else 0
            statuses.append("REJECTED")
            reasons.append(f"Vốn ví gốc ${r['entry_cost_usdc']:,.0f} < ${min_whale_req:,.0f} (Lệnh bạn ${raw_bet:.2f} < ${min_user_bet:.2f})")
            continue

        # Quy tắc 2: Bộ lọc dải xác suất
        if block_50_60 and (0.50 <= p < 0.60):
            statuses.append("REJECTED")
            reasons.append(f"Dải cấm 50%–60% (Giá ví gốc {p*100:.1f}¢)")
            continue

        if p > (max_fav_prob / 100.0):
            statuses.append("REJECTED")
            reasons.append(f"Dải cấm >{max_fav_prob:.0f}% (Biên lãi mỏng: {p*100:.1f}¢)")
            continue

        # Quy tắc 4: Kiểm soát trượt giá
        if slip_c > max_slippage_cents:
            statuses.append("SLIPPAGE_WARN")
            reasons.append(f"Vượt trần trượt giá (+{slip_c:.1f}¢ > +{max_slippage_cents:.1f}¢)")
            continue

        statuses.append("VALID")
        reasons.append("Đạt đủ 5 quy tắc")

    df["signal_group"] = statuses
    df["reason"] = reasons
    return df


# Cần import numpy cho tính toán vectorized
import numpy as np

# ======================== SIDEBAR CẤU HÌNH ========================
with st.sidebar:
    st.header("⚙️ Tham số 5 Quy Tắc")
    wallet_input = st.text_input("Địa chỉ ví theo dõi", value=DEFAULT_WALLET)

    multiplier = st.number_input(
        "Hệ số nhân quy mô",
        min_value=0.00001,
        max_value=1.0,
        value=0.00025,
        step=0.00005,
        format="%.5f",
    )

    col_s1, col_s2 = st.columns(2)
    min_user_bet = col_s1.number_input("Lệnh tối thiểu ($)", min_value=0.1, value=1.00, step=0.5)
    max_user_cap = col_s2.number_input("Trần tối đa ($)", min_value=1.0, value=10.00, step=1.0)

    max_slippage_cents = st.slider(
        "Trượt giá tối đa cho phép (cent)",
        min_value=0.5,
        max_value=10.0,
        value=2.5,
        step=0.5,
    )

    block_50_60 = st.checkbox("Chặn dải lỗ 50% – 60%", value=True)
    max_fav_prob = st.slider("Chặn kèo chắc ăn trên (%)", min_value=70, max_value=99, value=85, step=5)

    st.divider()
    st.caption(
        f"**Ngưỡng kích hoạt ví gốc:** Vốn vào của họ phải từ **${min_user_bet / multiplier:,.0f}** "
        f"đến **${max_user_cap / multiplier:,.0f}** (đạt trần ${max_user_cap:.0f})."
    )

# ======================== MAIN UI ========================
st.title("⚡ Trạm Quét Lệnh Copy-Trade (Áp dụng sẵn 5 Quy Tắc)")

top_col1, top_col2 = st.columns([1.2, 3])
with top_col1:
    scan_clicked = st.button("🔍 QUÉT LỆNH HỢP LỆ NGAY", type="primary", use_container_width=True)

if "scan_data" not in st.session_state or scan_clicked:
    with st.spinner("Đang quét vị thế OPEN & REDEEMABLE trực tiếp từ Polymarket API v2..."):
        try:
            open_rows = fetch_positions_v2(wallet_input, "OPEN")
            redeem_rows = fetch_positions_v2(wallet_input, "REDEEMABLE")
            st.session_state["scan_data"] = (open_rows, redeem_rows, datetime.now().strftime("%H:%M:%S %d/%m/%Y"))
        except Exception as e:
            st.error(f"Lỗi gọi API Polymarket v2: {e}")
            st.stop()

open_rows, redeem_rows, last_scan_str = st.session_state["scan_data"]
top_col2.caption(f"Cập nhật lần cuối lúc: **{last_scan_str}** | Tổng vị thế đang mở của ví: **{len(open_rows)}**")

evaluated_df = evaluate_positions(
    open_rows,
    multiplier=multiplier,
    min_user_bet=min_user_bet,
    max_user_cap=max_user_cap,
    max_slippage_cents=max_slippage_cents,
    block_50_60=block_50_60,
    max_fav_prob=float(max_fav_prob),
)

if evaluated_df.empty:
    st.info("Hiện tại ví mục tiêu không có vị thế nào đang mở (`OPEN`).")
    st.stop()

valid_df = evaluated_df[evaluated_df["signal_group"] == "VALID"].sort_values("final_user_bet", ascending=False)
slip_df = evaluated_df[evaluated_df["signal_group"] == "SLIPPAGE_WARN"].sort_values("final_user_bet", ascending=False)
rej_df = evaluated_df[evaluated_df["signal_group"] == "REJECTED"].sort_values("entry_cost_usdc", ascending=False)

# --- CHỈ SỐ TỔNG QUAN NHANH ---
m1, m2, m3, m4 = st.columns(4)
m1.metric("🟢 Kèo hợp lệ cần vào", f"{len(valid_df)} kèo", f"Tổng vốn cần: ${valid_df['final_user_bet'].sum():.2f}")
m2.metric("🟡 Kèo đúng vùng nhưng trượt giá", f"{len(slip_df)} kèo", "Chờ giá hồi về trần mới vào")
m3.metric("⚪ Kèo bị bộ lọc loại bỏ", f"{len(rej_df)} kèo", "Dưới $4k hoặc sai dải giá")
m4.metric("💰 Kèo vừa thắng chờ Redeem", f"{len(redeem_rows)} kèo", "Nhắc bấm Claim trên web")

st.divider()

# --- BẢNG 1: KÈO HỢP LỆ VÀO LỆNH NGAY ---
st.subheader(f"🟢 1. Danh sách Kèo Hợp Lệ — Vào Lệnh Ngay ({len(valid_df)})")
if valid_df.empty:
    st.info("Chưa có kèo nào đạt đủ cả 5 quy tắc ở thời điểm hiện tại. Hãy quay lại quét trước giờ các trận CS2 diễn ra.")
else:
    disp_valid = valid_df[
        ["title", "outcome", "final_user_bet", "avg_price", "cur_price", "slippage_cents", "max_limit_cents", "entry_cost_usdc", "last_buy_ago", "url"]
    ].copy()
    disp_valid["avg_price"] = disp_valid["avg_price"] * 100.0
    disp_valid["cur_price"] = disp_valid["cur_price"] * 100.0

    st.dataframe(
        disp_valid,
        use_container_width=True,
        hide_index=True,
        column_config={
            "title": st.column_config.TextColumn("Trận đấu (Market)", width="large"),
            "outcome": st.column_config.TextColumn("Cửa chọn"),
            "final_user_bet": st.column_config.NumberColumn("💵 Tiền bạn vào ($)", format="$%.2f"),
            "avg_price": st.column_config.NumberColumn("Giá ví gốc", format="%.1f¢"),
            "cur_price": st.column_config.NumberColumn("Giá hiện tại", format="%.1f¢"),
            "slippage_cents": st.column_config.NumberColumn("Trượt giá", format="%+.1f¢"),
            "max_limit_cents": st.column_config.NumberColumn("🛑 Giá trần tối đa", format="%.1f¢"),
            "entry_cost_usdc": st.column_config.NumberColumn("Vốn ví gốc ($)", format="$%,.0f"),
            "last_buy_ago": st.column_config.TextColumn("Lệnh cuối"),
            "url": st.column_config.LinkColumn("Đặt lệnh", display_text="Mở Polymarket"),
        },
    )

# --- BẢNG 2: KÈO QUÁ TRƯỢT GIÁ ---
if not slip_df.empty:
    st.subheader(f"🟡 2. Kèo Đạt Ngưỡng Vốn & Dải Giá Nhưng Đang Quá Trượt Giá ({len(slip_df)})")
    st.caption("Không đuổi giá thị trường ở các kèo này; chỉ đặt lệnh Limit ở cột **Giá trần tối đa** nếu muốn chờ khớp.")
    disp_slip = slip_df[
        ["title", "outcome", "final_user_bet", "avg_price", "cur_price", "slippage_cents", "max_limit_cents", "entry_cost_usdc", "last_buy_ago", "url"]
    ].copy()
    disp_slip["avg_price"] = disp_slip["avg_price"] * 100.0
    disp_slip["cur_price"] = disp_slip["cur_price"] * 100.0

    st.dataframe(
        disp_slip,
        use_container_width=True,
        hide_index=True,
        column_config={
            "title": st.column_config.TextColumn("Trận đấu (Market)", width="large"),
            "outcome": st.column_config.TextColumn("Cửa chọn"),
            "final_user_bet": st.column_config.NumberColumn("💵 Tiền bạn vào ($)", format="$%.2f"),
            "avg_price": st.column_config.NumberColumn("Giá ví gốc", format="%.1f¢"),
            "cur_price": st.column_config.NumberColumn("Giá hiện tại", format="%.1f¢"),
            "slippage_cents": st.column_config.NumberColumn("Trượt giá", format="%+.1f¢"),
            "max_limit_cents": st.column_config.NumberColumn("🛑 Giá trần tối đa", format="%.1f¢"),
            "entry_cost_usdc": st.column_config.NumberColumn("Vốn ví gốc ($)", format="$%,.0f"),
            "last_buy_ago": st.column_config.TextColumn("Lệnh cuối"),
            "url": st.column_config.LinkColumn("Xem kèo", display_text="Mở Polymarket"),
        },
    )

# --- BẢNG 3: KÈO BỊ LOẠI & KÈO CHỜ REDEEM ---
with st.expander(f"⚪ Xem {len(rej_df)} vị thế đang mở bị loại bởi Bộ 5 Quy Tắc"):
    disp_rej = rej_df[["reason", "title", "outcome", "avg_price", "entry_cost_usdc", "raw_user_bet", "last_buy_ago"]].copy()
    disp_rej["avg_price"] = disp_rej["avg_price"] * 100.0
    st.dataframe(
        disp_rej,
        use_container_width=True,
        hide_index=True,
        column_config={
            "reason": st.column_config.TextColumn("Lý do loại", width="medium"),
            "title": st.column_config.TextColumn("Trận đấu", width="large"),
            "outcome": st.column_config.TextColumn("Cửa"),
            "avg_price": st.column_config.NumberColumn("Giá ví gốc", format="%.1f¢"),
            "entry_cost_usdc": st.column_config.NumberColumn("Vốn ví gốc ($)", format="$%,.2f"),
            "raw_user_bet": st.column_config.NumberColumn("Quy đổi x0.00025", format="$%.2f"),
            "last_buy_ago": st.column_config.TextColumn("Lệnh cuối"),
        },
    )

if redeem_rows:
    with st.expander(f"💰 Xem {len(redeem_rows)} kèo của ví vừa ngã ngũ (REDEEMABLE) — Nhắc bạn bấm Claim nếu đã theo"):
        red_df = pd.DataFrame(redeem_rows)
        cols_r = [c for c in ["title", "outcome", "avg_price", "unrealized_pnl"] if c in red_df.columns]
        if cols_r:
            st.dataframe(red_df[cols_r], use_container_width=True, hide_index=True)
