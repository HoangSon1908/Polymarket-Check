import streamlit as st
import requests
import pandas as pd

st.set_page_config(
    page_title="Polymarket Wallet Analyzer (v2)",
    page_icon="📊",
    layout="wide",
)

POSITIONS_API_URL = "https://data-api.polymarket.com/v2/positions"
SETTLED_STATUSES = ["CLOSED", "REDEEMABLE", "REDEEMABLE_LOST"]


@st.cache_data(ttl=300, show_spinner="Đang kéo dữ liệu từ Polymarket Data API v2...")
def fetch_all_positions(wallet: str) -> tuple[pd.DataFrame, dict]:
    all_rows = []
    params = {
        "user": wallet.strip(),
        "limit": 500,
    }
    max_pages = 100

    for _ in range(max_pages):
        res = requests.get(POSITIONS_API_URL, params=params, timeout=15)
        res.raise_for_status()
        payload = res.json()

        # Theo contract v2: data có thể là list hoặc null
        batch = payload.get("data") or []
        all_rows.extend(batch)

        pagination = payload.get("pagination") or {}
        next_cursor = pagination.get("next_cursor")
        has_more = pagination.get("has_more", False)

        # Dừng khi next_cursor là null hoặc has_more là False
        if not next_cursor or not has_more:
            break

        # Pagination v2 chỉ dùng cursor, giữ nguyên anchor user, không gửi offset
        params = {
            "user": wallet.strip(),
            "cursor": next_cursor,
        }

    if not all_rows:
        return pd.DataFrame(), {}

    sample_raw = all_rows[0]
    df = pd.DataFrame(all_rows)

    def get_col(candidates: list[str], default=None) -> pd.Series:
        for c in candidates:
            if c in df.columns:
                return df[c]
        return pd.Series([default] * len(df), index=df.index)

    df["status"] = get_col(["status"], default="UNKNOWN").astype(str).str.upper()
    df["title"] = get_col(["title", "question"], default="N/A").astype(str)
    df["outcome"] = get_col(["outcome"], default="").astype(str)

    df["avg_price"] = pd.to_numeric(get_col(["avg_price", "avgPrice"]), errors="coerce").fillna(0.0)
    df["current_size"] = pd.to_numeric(get_col(["current_size", "size"]), errors="coerce").fillna(0.0)
    df["entry_cost_usdc"] = pd.to_numeric(get_col(["entry_cost_usdc"]), errors="coerce").fillna(0.0)
    df["current_value"] = pd.to_numeric(get_col(["current_value"]), errors="coerce").fillna(0.0)
    df["realized_pnl"] = pd.to_numeric(get_col(["realized_pnl", "realizedPnl"]), errors="coerce").fillna(0.0)
    df["unrealized_pnl"] = pd.to_numeric(get_col(["unrealized_pnl", "unrealizedPnl"]), errors="coerce").fillna(0.0)

    # Tổng PnL gồm cả phần đã chốt (realized) và phần chờ claim sau khi ngã ngũ (unrealized)
    df["total_pnl"] = df["realized_pnl"] + df["unrealized_pnl"]
    df["is_win"] = df["total_pnl"] > 0

    # Tính vốn vào: ưu tiên tổng vốn ban đầu nếu API có trả về, fallback sang entry_cost_usdc
    total_bought = pd.to_numeric(get_col(["total_bought", "totalBought"]), errors="coerce")
    initial_value = pd.to_numeric(get_col(["initial_value", "initialValue", "bought_usdc"]), errors="coerce")

    cost = initial_value.copy()
    cost = cost.where(cost.notna() & (cost > 0), total_bought * df["avg_price"])
    cost = cost.where(cost.notna() & (cost > 0), df["entry_cost_usdc"])
    df["cost"] = pd.to_numeric(cost.where(cost > 0, other=pd.NA), errors="coerce")

    df["roi_pct"] = (df["total_pnl"] / df["cost"]) * 100

    raw_time = get_col(["closed_at", "updated_at", "timestamp", "end_date", "created_at"], default=None)
    if pd.api.types.is_numeric_dtype(raw_time):
        df["time"] = pd.to_datetime(raw_time, unit="s", errors="coerce")
    else:
        df["time"] = pd.to_datetime(raw_time, errors="coerce")

    slug_col = get_col(["event_slug", "eventSlug", "slug"], default="")
    df["url"] = slug_col.apply(
        lambda s: f"https://polymarket.com/event/{s}" if pd.notna(s) and str(s).strip() else ""
    )

    return df, sample_raw


def summarize_bucket(sub_df: pd.DataFrame) -> dict:
    total = len(sub_df)
    if total == 0:
        return {"total": 0, "wins": 0, "win_rate": 0.0, "cost": 0.0, "pnl": 0.0, "roi": None}

    wins = int(sub_df["is_win"].sum())
    pnl = float(sub_df["total_pnl"].sum())

    valid_cost_df = sub_df.dropna(subset=["cost"])
    known_cost = float(valid_cost_df["cost"].sum())
    known_pnl = float(valid_cost_df["total_pnl"].sum())
    roi = (known_pnl / known_cost * 100) if known_cost > 0 else None

    return {
        "total": total,
        "wins": wins,
        "win_rate": (wins / total) * 100,
        "cost": float(sub_df["cost"].sum(skipna=True)),
        "pnl": pnl,
        "roi": roi,
    }


# --- SIDEBAR ---
with st.sidebar:
    st.header("Cấu hình phân tích")
    wallet_input = st.text_input(
        "Địa chỉ Proxy Wallet (0x...)",
        placeholder="0x1234...abcd",
    )

    threshold_pct = st.slider(
        "Ngưỡng chia xác suất (%)",
        min_value=5,
        max_value=95,
        value=50,
        step=5,
    )

    selected_statuses = st.multiselect(
        "Trạng thái vị thế tính thống kê",
        options=["CLOSED", "REDEEMABLE", "REDEEMABLE_LOST", "OPEN", "MERGEABLE"],
        default=SETTLED_STATUSES,
        help="Mặc định lấy 3 trạng thái đã kết thúc: CLOSED (đã đóng/claim), REDEEMABLE (thắng chờ claim), REDEEMABLE_LOST (thua).",
    )

    min_cost = st.number_input("Lọc vốn tối thiểu mỗi lệnh ($)", min_value=0.0, value=0.0, step=10.0)

    if st.button("Làm mới dữ liệu", use_container_width=True):
        st.cache_data.clear()

# --- MAIN UI ---
st.title("📊 Phân tích hiệu suất ví Polymarket (API v2)")

if not wallet_input:
    st.info("Nhập địa chỉ ví `0x...` ở thanh bên trái để bắt đầu thống kê.")
    st.stop()

try:
    raw_df, sample_json = fetch_all_positions(wallet_input)
except Exception as e:
    st.error(f"Lỗi khi gọi API Polymarket v2: {e}")
    st.stop()

if raw_df.empty:
    st.warning("API trả về dữ liệu trống cho địa chỉ ví này.")
    st.stop()

# Lọc theo trạng thái & mức vốn
df = raw_df.copy()
if selected_statuses:
    df = df[df["status"].isin(selected_statuses)]

if min_cost > 0:
    df = df[df["cost"].fillna(0) >= min_cost]

if df.empty:
    st.warning("Không có vị thế nào khớp với bộ lọc hiện tại.")
    st.stop()

thresh = threshold_pct / 100.0
under_df = df[df["avg_price"] < thresh].copy()
over_df = df[df["avg_price"] > thresh].copy()
exact_df = df[df["avg_price"] == thresh].copy()

under_stats = summarize_bucket(under_df)
over_stats = summarize_bucket(over_df)

col_left, col_right = st.columns(2)

with col_left:
    st.subheader(f"🔵 Kèo dưới {threshold_pct}% (Underdog)")
    m1, m2, m3 = st.columns(3)
    m1.metric("Tỷ lệ thắng", f"{under_stats['win_rate']:.1f}%", f"{under_stats['wins']}/{under_stats['total']} kèo")
    roi_under_str = f"ROI: {under_stats['roi']:+.1f}%" if under_stats["roi"] is not None else "ROI: N/A"
    m2.metric("Lời / Lỗ ròng", f"${under_stats['pnl']:+,.2f}", roi_under_str)
    m3.metric("Vốn ghi nhận", f"${under_stats['cost']:,.2f}")

with col_right:
    st.subheader(f"🟠 Kèo trên {threshold_pct}% (Favorite)")
    m1, m2, m3 = st.columns(3)
    m1.metric("Tỷ lệ thắng", f"{over_stats['win_rate']:.1f}%", f"{over_stats['wins']}/{over_stats['total']} kèo")
    roi_over_str = f"ROI: {over_stats['roi']:+.1f}%" if over_stats["roi"] is not None else "ROI: N/A"
    m2.metric("Lời / Lỗ ròng", f"${over_stats['pnl']:+,.2f}", roi_over_str)
    m3.metric("Vốn ghi nhận", f"${over_stats['cost']:,.2f}")

if len(exact_df) > 0:
    st.caption(f"Lưu ý: Có {len(exact_df)} vị thế khớp đúng mức {threshold_pct}% không nằm trong 2 cột trên.")

st.divider()

if df["time"].notna().any():
    st.subheader("📈 Biểu đồ PnL tích lũy theo ngày")
    chart_df = df.dropna(subset=["time"]).copy()
    chart_df["date"] = chart_df["time"].dt.date
    chart_df["Nhóm"] = chart_df["avg_price"].apply(
        lambda p: f"Dưới {threshold_pct}%" if p < thresh else (f"Trên {threshold_pct}%" if p > thresh else f"Đúng {threshold_pct}%")
    )
    daily_pnl = (
        chart_df.pivot_table(index="date", columns="Nhóm", values="total_pnl", aggfunc="sum")
        .fillna(0)
        .cumsum()
    )
    st.line_chart(daily_pnl)

st.subheader("📋 Chi tiết từng vị thế")
tab_under, tab_over, tab_all = st.tabs([
    f"Dưới {threshold_pct}% ({len(under_df)})",
    f"Trên {threshold_pct}% ({len(over_df)})",
    f"Tất cả ({len(df)})",
])


def render_table(target_df: pd.DataFrame, key_suffix: str):
    if target_df.empty:
        st.write("Không có dữ liệu.")
        return

    display_df = target_df[
        ["status", "title", "outcome", "avg_price", "cost", "realized_pnl", "unrealized_pnl", "total_pnl", "roi_pct", "url"]
    ].copy()
    display_df["avg_price"] = display_df["avg_price"] * 100

    st.dataframe(
        display_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "status": st.column_config.TextColumn("Trạng thái"),
            "title": st.column_config.TextColumn("Market", width="large"),
            "outcome": st.column_config.TextColumn("Cửa"),
            "avg_price": st.column_config.NumberColumn("Giá mua TB", format="%.1f%%"),
            "cost": st.column_config.NumberColumn("Vốn ($)", format="$%.2f"),
            "realized_pnl": st.column_config.NumberColumn("Realized PnL", format="$%+.2f"),
            "unrealized_pnl": st.column_config.NumberColumn("Unrealized PnL", format="$%+.2f"),
            "total_pnl": st.column_config.NumberColumn("Tổng PnL ($)", format="$%+.2f"),
            "roi_pct": st.column_config.NumberColumn("ROI", format="%+.1f%%"),
            "url": st.column_config.LinkColumn("Link", display_text="Xem"),
        },
    )

    csv = target_df.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        label="Tải xuống CSV",
        data=csv,
        file_name=f"polymarket_v2_{key_suffix}.csv",
        mime="text/csv",
        key=f"dl_{key_suffix}",
    )


with tab_under:
    render_table(under_df, "under")
with tab_over:
    render_table(over_df, "over")
with tab_all:
    render_table(df, "all")

with st.expander("Xem cấu trúc JSON gốc từ /v2/positions"):
    st.json(sample_json)
