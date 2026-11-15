import time
import requests
import numpy as np
import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="Polymarket Deep Edge Analyzer (v2)",
    page_icon="🎯",
    layout="wide",
)

BASE_URL = "https://data-api.polymarket.com/v2"
POSITIONS_URL = f"{BASE_URL}/positions"
ACTIVITY_URL = f"{BASE_URL}/activity"

ALL_STATUSES = ["CLOSED", "REDEEMABLE", "REDEEMABLE_LOST", "OPEN", "MERGEABLE"]
SETTLED_STATUSES = ["CLOSED", "REDEEMABLE", "REDEEMABLE_LOST"]


def get_with_retry(url: str, params: dict, max_retries: int = 5) -> dict:
    """Gọi API v2 có tự động chờ khi gặp giới hạn tốc độ (HTTP 429)."""
    for attempt in range(max_retries):
        res = requests.get(url, params=params, timeout=20)
        if res.status_code == 429:
            retry_after = res.headers.get("Retry-After")
            wait_sec = float(retry_after) if retry_after and retry_after.isdigit() else (2.0 * (attempt + 1))
            time.sleep(min(wait_sec, 15.0))
            continue
        res.raise_for_status()
        return res.json()
    raise RuntimeError(f"Vượt quá số lần thử lại (429 Rate Limit) tại {url}")


def fetch_positions_all_statuses(wallet: str, status_box) -> tuple[list[dict], list[str]]:
    all_rows = []
    warnings = []

    for status_val in ALL_STATUSES:
        status_box.write(f"• Đang tải `/v2/positions` (status = `{status_val}`)...")
        params = {"user": wallet, "status": status_val, "limit": 500}
        page = 0

        while True:
            page += 1
            try:
                payload = get_with_retry(POSITIONS_URL, params)
            except requests.HTTPError as e:
                code = e.response.status_code if e.response is not None else "ERR"
                warnings.append(f"positions[{status_val}] HTTP {code}")
                break

            batch = payload.get("data") or []
            for item in batch:
                if not item.get("status"):
                    item["status"] = status_val
            all_rows.extend(batch)

            pagination = payload.get("pagination") or {}
            next_cursor = pagination.get("next_cursor")
            has_more = pagination.get("has_more", False)

            if not next_cursor or not has_more:
                break

            # Pagination v2 chỉ dùng cursor, giữ nguyên anchor user & status
            params = {
                "user": wallet,
                "status": status_val,
                "cursor": next_cursor,
            }

    return all_rows, warnings


def fetch_full_activity(wallet: str, status_box) -> tuple[list[dict], list[str]]:
    all_events = []
    warnings = []
    params = {"user": wallet, "limit": 500}
    page = 0

    while True:
        page += 1
        status_box.write(f"• Đang tải `/v2/activity` (trang {page} — đã lấy {len(all_events):,} sự kiện)...")
        try:
            payload = get_with_retry(ACTIVITY_URL, params)
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else "ERR"
            warnings.append(f"activity HTTP {code}")
            break

        batch = payload.get("data") or []
        all_events.extend(batch)

        pagination = payload.get("pagination") or {}
        next_cursor = pagination.get("next_cursor")
        has_more = pagination.get("has_more", False)

        if not next_cursor or not has_more:
            break

        # Feed v2 yêu cầu giữ nguyên filter user khi truyền cursor
        params = {
            "user": wallet,
            "cursor": next_cursor,
        }

    return all_events, warnings


def aggregate_activity_metrics(events: list[dict]) -> tuple[dict, dict]:
    """
    Tổng hợp lịch sử lệnh theo token_id và (condition_id, outcome) để tính:
    - Vốn mua thực tế (actual_buy_usdc)
    - Tiền bán chốt sớm (actual_sell_usdc) & tiền đáo hạn (actual_redeem_usdc)
    - Số lệnh mua/bán & thời gian nắm giữ thực tế
    """
    if not events:
        return {}, {}

    ev = pd.DataFrame(events)

    def col_or(names: list[str], default=None) -> pd.Series:
        for n in names:
            if n in ev.columns:
                return ev[n]
        return pd.Series([default] * len(ev), index=ev.index)

    ev["type"] = col_or(["type", "activity_type"], default="TRADE").astype(str).str.upper()
    ev["side"] = col_or(["side"], default="").astype(str).str.upper()
    ev["condition_id"] = col_or(["condition_id", "conditionId"], default="").fillna("").astype(str)
    ev["token_id"] = col_or(["token_id", "tokenId", "asset", "asset_id"], default="").fillna("").astype(str)
    ev["outcome"] = col_or(["outcome"], default="").fillna("").astype(str)

    ev["size"] = pd.to_numeric(col_or(["size", "shares", "amount"]), errors="coerce").fillna(0.0)
    ev["price"] = pd.to_numeric(col_or(["price"]), errors="coerce").fillna(0.0)
    raw_usdc = pd.to_numeric(col_or(["usdc_size", "usdcSize", "amount_usdc"]), errors="coerce")
    ev["usdc"] = raw_usdc.where(raw_usdc.notna() & (raw_usdc > 0), ev["price"] * ev["size"]).fillna(0.0)

    raw_ts = col_or(["timestamp", "created_at", "time"], default=None)
    if pd.api.types.is_numeric_dtype(raw_ts):
        ev["ts"] = pd.to_datetime(raw_ts, unit="s", errors="coerce")
    else:
        ev["ts"] = pd.to_datetime(raw_ts, errors="coerce")

    def summarize_group(grp: pd.DataFrame) -> dict:
        buys = grp[(grp["type"] == "TRADE") & (grp["side"] == "BUY")]
        sells = grp[(grp["type"] == "TRADE") & (grp["side"] == "SELL")]
        redeems = grp[grp["type"] == "REDEEM"]
        merges = grp[grp["type"] == "MERGE"]

        buy_usdc = float(buys["usdc"].sum())
        buy_shares = float(buys["size"].sum())
        sell_usdc = float(sells["usdc"].sum())
        sell_shares = float(sells["size"].sum())
        redeem_usdc = float(redeems["usdc"].sum())
        merge_usdc = float(merges["usdc"].sum())

        buy_count = int(len(buys))
        sell_count = int(len(sells))

        first_buy_ts = buys["ts"].min() if not buys.empty else grp["ts"].min()
        last_ts = grp["ts"].max()
        hold_hours = np.nan
        if pd.notna(first_buy_ts) and pd.notna(last_ts) and last_ts >= first_buy_ts:
            hold_hours = (last_ts - first_buy_ts).total_seconds() / 3600.0

        return {
            "act_buy_usdc": buy_usdc,
            "act_buy_shares": buy_shares,
            "act_sell_usdc": sell_usdc,
            "act_sell_shares": sell_shares,
            "act_redeem_usdc": redeem_usdc,
            "act_merge_usdc": merge_usdc,
            "buy_count": buy_count,
            "sell_count": sell_count,
            "total_trades": buy_count + sell_count,
            "first_buy_ts": first_buy_ts,
            "last_act_ts": last_ts,
            "hold_hours": hold_hours,
        }

    by_token = {}
    valid_token_df = ev[ev["token_id"] != ""]
    for tid, grp in valid_token_df.groupby("token_id"):
        by_token[tid] = summarize_group(grp)

    by_cond_out = {}
    valid_co_df = ev[(ev["condition_id"] != "") & (ev["outcome"] != "")]
    for (cid, out), grp in valid_co_df.groupby(["condition_id", "outcome"]):
        by_cond_out[(cid, out)] = summarize_group(grp)

    return by_token, by_cond_out


@st.cache_data(ttl=600, show_spinner=False)
def load_reconciled_wallet_data(wallet: str) -> tuple[pd.DataFrame, list[str], int]:
    wallet_clean = wallet.strip()
    with st.status("Đang tải và đối soát toàn bộ dữ liệu on-chain (API v2)...", expanded=True) as status_box:
        pos_rows, pos_warn = fetch_positions_all_statuses(wallet_clean, status_box)
        act_rows, act_warn = fetch_full_activity(wallet_clean, status_box)
        status_box.write("• Đang ghép nối vị thế với lịch sử khớp lệnh thực tế...")

        warnings = pos_warn + act_warn
        if not pos_rows:
            status_box.update(label="Hoàn tất (Không tìm thấy vị thế)", state="complete", expanded=False)
            return pd.DataFrame(), warnings, len(act_rows)

        df = pd.DataFrame(pos_rows)
        dedup_cols = [c for c in ["condition_id", "token_id", "outcome", "status"] if c in df.columns]
        if dedup_cols:
            df = df.drop_duplicates(subset=dedup_cols).reset_index(drop=True)

        def col_or(names: list[str], default=None) -> pd.Series:
            for n in names:
                if n in df.columns:
                    return df[n]
            return pd.Series([default] * len(df), index=df.index)

        df["status"] = col_or(["status"], default="UNKNOWN").astype(str).str.upper()
        df["condition_id"] = col_or(["condition_id"], default="").fillna("").astype(str)
        df["token_id"] = col_or(["token_id"], default="").fillna("").astype(str)
        df["title"] = col_or(["title", "question"], default="N/A").astype(str)
        df["outcome"] = col_or(["outcome"], default="").astype(str)

        df["avg_price"] = pd.to_numeric(col_or(["avg_price"]), errors="coerce").fillna(0.0)
        df["current_size"] = pd.to_numeric(col_or(["current_size"]), errors="coerce").fillna(0.0)
        df["entry_cost_usdc"] = pd.to_numeric(col_or(["entry_cost_usdc"]), errors="coerce").fillna(0.0)
        df["current_value"] = pd.to_numeric(col_or(["current_value"]), errors="coerce").fillna(0.0)
        df["realized_pnl"] = pd.to_numeric(col_or(["realized_pnl"]), errors="coerce").fillna(0.0)
        df["unrealized_pnl"] = pd.to_numeric(col_or(["unrealized_pnl"]), errors="coerce").fillna(0.0)

        # Quy chuẩn v2: Tổng PnL = realized_pnl + unrealized_pnl
        df["total_pnl"] = df["realized_pnl"] + df["unrealized_pnl"]
        df["is_win"] = df["total_pnl"] > 0

        # Ghép dữ liệu từ /v2/activity
        by_token, by_cond_out = aggregate_activity_metrics(act_rows)

        matched_records = []
        for _, row in df.iterrows():
            rec = by_token.get(row["token_id"])
            if rec is None:
                rec = by_cond_out.get((row["condition_id"], row["outcome"]))
            matched_records.append(rec or {})

        act_df = pd.DataFrame(matched_records, index=df.index)
        for col in [
            "act_buy_usdc", "act_buy_shares", "act_sell_usdc", "act_sell_shares",
            "act_redeem_usdc", "act_merge_usdc", "buy_count", "sell_count",
            "total_trades", "hold_hours",
        ]:
            df[col] = pd.to_numeric(act_df.get(col, np.nan), errors="coerce")

        df["first_buy_ts"] = pd.to_datetime(act_df.get("first_buy_ts", pd.NaT), errors="coerce")
        df["last_act_ts"] = pd.to_datetime(act_df.get("last_act_ts", pd.NaT), errors="coerce")

        # Giải quyết Điểm mù 4: Xác định Vốn thực tế (Ưu tiên Activity -> Entry Cost -> Ước tính)
        def resolve_true_cost(r):
            if pd.notna(r["act_buy_usdc"]) and r["act_buy_usdc"] > 0:
                return r["act_buy_usdc"], "Activity (Chính xác)"
            if r["entry_cost_usdc"] > 0:
                return r["entry_cost_usdc"], "Position (Đang giữ)"
            pnl = r["total_pnl"]
            p = r["avg_price"]
            if pnl < 0:
                return abs(pnl), "Ước tính từ PnL"
            if pnl > 0 and 0 < p < 1:
                return pnl * p / (1.0 - p), "Ước tính từ PnL"
            return np.nan, "Không rõ"

        cost_res = df.apply(resolve_true_cost, axis=1)
        df["true_cost"] = [c[0] for c in cost_res]
        df["cost_source"] = [c[1] for c in cost_res]
        df["true_roi_pct"] = (df["total_pnl"] / df["true_cost"]) * 100.0

        # Giải quyết Điểm mù 3: Phân loại hành vi (Giữ đáo hạn vs Bán sớm/Lướt sóng)
        def classify_exit(r):
            st_val = r["status"]
            if st_val == "OPEN":
                return "Đang mở (OPEN)"
            buy_sh = r["act_buy_shares"] if pd.notna(r["act_buy_shares"]) else 0.0
            sell_sh = r["act_sell_shares"] if pd.notna(r["act_sell_shares"]) else 0.0
            redeem_usdc = r["act_redeem_usdc"] if pd.notna(r["act_redeem_usdc"]) else 0.0

            if st_val in ("REDEEMABLE", "REDEEMABLE_LOST"):
                if buy_sh > 0 and sell_sh >= 0.2 * buy_sh:
                    return "Kết hợp (Bán một phần + Đáo hạn)"
                return "Giữ đến đáo hạn"

            # Trường hợp CLOSED hoặc MERGEABLE
            if buy_sh > 0:
                sell_ratio = sell_sh / buy_sh
                if sell_ratio >= 0.85:
                    return "Chốt/Cắt sớm (Swing)"
                if sell_ratio <= 0.15:
                    return "Giữ đến đáo hạn"
                return "Kết hợp (Bán một phần + Đáo hạn)"

            if redeem_usdc > 0:
                return "Giữ đến đáo hạn"
            return "Không đủ dữ liệu lệnh"

        df["exit_style"] = df.apply(classify_exit, axis=1)

        # Thời gian đóng/cập nhật chuẩn xác nhất
        raw_pos_time = col_or(["closed_at", "updated_at", "timestamp", "end_date"], default=None)
        if pd.api.types.is_numeric_dtype(raw_pos_time):
            pos_ts = pd.to_datetime(raw_pos_time, unit="s", errors="coerce")
        else:
            pos_ts = pd.to_datetime(raw_pos_time, errors="coerce")
        df["time"] = df["last_act_ts"].where(df["last_act_ts"].notna(), pos_ts)

        slug_col = col_or(["event_slug", "eventSlug", "slug"], default="")
        df["url"] = slug_col.apply(
            lambda s: f"https://polymarket.com/event/{s}" if pd.notna(s) and str(s).strip() else ""
        )

        status_box.update(
            label=f"Hoàn tất đối soát {len(df):,} vị thế & {len(act_rows):,} sự kiện giao dịch!",
            state="complete",
            expanded=False,
        )
        return df, warnings, len(act_rows)


def compute_edge_metrics(sub: pd.DataFrame) -> dict:
    n = len(sub)
    if n == 0:
        return {
            "n": 0, "wins": 0, "win_rate": 0.0, "cap_win_rate": 0.0,
            "simple_entry": 0.0, "weighted_entry": 0.0,
            "simple_edge": 0.0, "weighted_edge": 0.0,
            "cost": 0.0, "pnl": 0.0, "roi": 0.0,
            "profit_factor": np.nan, "pnl_ex_top5": 0.0, "top5_share": 0.0,
            "max_dd": 0.0, "avg_trades": np.nan, "median_hold_h": np.nan,
        }

    wins = int(sub["is_win"].sum())
    win_rate = (wins / n) * 100.0

    valid_cost = sub.dropna(subset=["true_cost"])
    total_cost = float(valid_cost["true_cost"].sum())
    win_cost = float(valid_cost.loc[valid_cost["is_win"], "true_cost"].sum())
    cap_win_rate = (win_cost / total_cost * 100.0) if total_cost > 0 else win_rate

    simple_entry = float(sub["avg_price"].mean() * 100.0)
    weighted_entry = (
        float((valid_cost["avg_price"] * valid_cost["true_cost"]).sum() / total_cost * 100.0)
        if total_cost > 0 else simple_entry
    )

    simple_edge = win_rate - simple_entry
    weighted_edge = cap_win_rate - weighted_entry

    total_pnl = float(sub["total_pnl"].sum())
    roi = (total_pnl / total_cost * 100.0) if total_cost > 0 else 0.0

    gross_profit = float(sub.loc[sub["total_pnl"] > 0, "total_pnl"].sum())
    gross_loss = float(sub.loc[sub["total_pnl"] < 0, "total_pnl"].sum())
    profit_factor = (gross_profit / abs(gross_loss)) if gross_loss < 0 else np.inf

    # Outlier / Top 5 concentration
    winning_pnls = sub.loc[sub["total_pnl"] > 0, "total_pnl"].sort_values(ascending=False)
    top5_sum = float(winning_pnls.head(5).sum())
    pnl_ex_top5 = total_pnl - top5_sum
    top5_share = (top5_sum / total_pnl * 100.0) if total_pnl > 0 else 0.0

    # Max Drawdown theo chuỗi thời gian
    ordered = sub.sort_values("time", na_position="first")
    cum_pnl = ordered["total_pnl"].cumsum()
    running_max = cum_pnl.cummax()
    max_dd = float((cum_pnl - running_max).min()) if not cum_pnl.empty else 0.0

    avg_trades = float(sub["total_trades"].mean()) if sub["total_trades"].notna().any() else np.nan
    median_hold_h = float(sub["hold_hours"].median()) if sub["hold_hours"].notna().any() else np.nan

    return {
        "n": n,
        "wins": wins,
        "win_rate": win_rate,
        "cap_win_rate": cap_win_rate,
        "simple_entry": simple_entry,
        "weighted_entry": weighted_entry,
        "simple_edge": simple_edge,
        "weighted_edge": weighted_edge,
        "cost": total_cost,
        "pnl": total_pnl,
        "roi": roi,
        "profit_factor": profit_factor,
        "pnl_ex_top5": pnl_ex_top5,
        "top5_share": top5_share,
        "max_dd": max_dd,
        "avg_trades": avg_trades,
        "median_hold_h": median_hold_h,
    }


# ======================== SIDEBAR ========================
with st.sidebar:
    st.header("Cấu hình kiểm toán ví")
    wallet_input = st.text_input(
        "Địa chỉ Proxy Wallet (0x...)",
        placeholder="0xc9bfc7...",
    )

    threshold_pct = st.slider(
        "Ngưỡng chia Underdog / Favorite (%)",
        min_value=5,
        max_value=95,
        value=50,
        step=5,
    )

    selected_statuses = st.multiselect(
        "Trạng thái vị thế tính thống kê",
        options=ALL_STATUSES,
        default=SETTLED_STATUSES,
    )

    exit_filter = st.multiselect(
        "Hành vi đóng lệnh (từ /v2/activity)",
        options=[
            "Giữ đến đáo hạn",
            "Chốt/Cắt sớm (Swing)",
            "Kết hợp (Bán một phần + Đáo hạn)",
            "Đang mở (OPEN)",
            "Không đủ dữ liệu lệnh",
        ],
        default=[
            "Giữ đến đáo hạn",
            "Chốt/Cắt sớm (Swing)",
            "Kết hợp (Bán một phần + Đáo hạn)",
            "Không đủ dữ liệu lệnh",
        ],
    )

    keyword_filter = st.text_input(
        "Lọc từ khóa ngách (cách nhau dấu phẩy)",
        placeholder="VD: Counter-Strike, Map Handicap",
        help="Để trống để xem tất cả. Nhập nhiều từ khóa để lọc OR.",
    )

    min_cost = st.number_input("Vốn mua tối thiểu mỗi kèo ($)", min_value=0.0, value=0.0, step=10.0)

    if st.button("Xóa cache & Tải lại từ đầu", use_container_width=True):
        st.cache_data.clear()

# ======================== MAIN UI ========================
st.title("🎯 Kiểm toán Edge & Hiệu suất Ví Polymarket (API v2)")

if not wallet_input:
    st.info("Nhập địa chỉ ví `0x...` ở thanh bên trái để bắt đầu phân tích chuyên sâu.")
    st.stop()

try:
    raw_df, api_warnings, total_events = load_reconciled_wallet_data(wallet_input)
except Exception as e:
    st.error(f"Lỗi khi kéo dữ liệu API v2: {e}")
    st.stop()

if api_warnings:
    st.caption("⚠️ Cảnh báo API: " + " | ".join(api_warnings))

if raw_df.empty:
    st.warning("Không tìm thấy vị thế nào cho địa chỉ ví này.")
    st.stop()

# Cảnh báo Điểm mù 1 (Hidden Floating Loss ở các lệnh OPEN)
open_df = raw_df[raw_df["status"] == "OPEN"]
if not open_df.empty:
    open_unrealized = float(open_df["unrealized_pnl"].sum())
    open_cost = float(open_df["entry_cost_usdc"].sum())
    msg = (
        f"**Trạng thái danh mục đang mở (`OPEN`):** Ví đang giữ **{len(open_df)}** vị thế "
        f"với tổng vốn **${open_cost:,.2f}** và PnL chưa chốt là **${open_unrealized:+,.2f}**."
    )
    if open_unrealized < -500:
        st.warning("🚨 " + msg)
    else:
        st.info("ℹ️ " + msg)

# Áp dụng các bộ lọc của người dùng
df = raw_df.copy()
if selected_statuses:
    df = df[df["status"].isin(selected_statuses)]
if exit_filter:
    df = df[df["exit_style"].isin(exit_filter)]
if keyword_filter.strip():
    kws = [k.strip() for k in keyword_filter.split(",") if k.strip()]
    pattern = "|".join(kws)
    df = df[df["title"].str.contains(pattern, case=False, na=False)]
if min_cost > 0:
    df = df[df["true_cost"].fillna(0) >= min_cost]

if df.empty:
    st.warning("Không có vị thế nào khớp với tổ hợp bộ lọc hiện tại.")
    st.stop()

# Tóm tắt chất lượng nguồn dữ liệu vốn
exact_pct = (df["cost_source"] == "Activity (Chính xác)").mean() * 100
st.caption(
    f"Đã khớp **{len(df):,}** vị thế từ **{total_events:,}** bản ghi `/v2/activity` | "
    f"Độ phủ vốn khớp lệnh thực tế: **{exact_pct:.1f}%**"
)

thresh = threshold_pct / 100.0
under_df = df[df["avg_price"] < thresh].copy()
over_df = df[df["avg_price"] > thresh].copy()

all_m = compute_edge_metrics(df)
under_m = compute_edge_metrics(under_df)
over_m = compute_edge_metrics(over_df)

# -------------------- PHẦN 1: CHỈ SỐ EDGE TỔNG QUAN --------------------
st.subheader("1️⃣ Đánh giá Lợi thế Thực tế (True Edge & Chất lượng Lợi nhuận)")
c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric(
    "PnL Ròng Tổng",
    f"${all_m['pnl']:+,.2f}",
    f"ROI thực: {all_m['roi']:+.1f}%",
)
c2.metric(
    "PnL loại trừ Top 5",
    f"${all_m['pnl_ex_top5']:+,.2f}",
    f"Top 5 chiếm {all_m['top5_share']:.1f}% lãi",
)
pf_str = f"{all_m['profit_factor']:.2f}" if np.isfinite(all_m["profit_factor"]) else "∞"
c3.metric(
    "Profit Factor",
    pf_str,
    f"Max DD: ${all_m['max_dd']:,.2f}",
)
c4.metric(
    "Win Rate (Số kèo / Vốn)",
    f"{all_m['win_rate']:.1f}% / {all_m['cap_win_rate']:.1f}%",
    f"{all_m['wins']}/{all_m['n']} kèo thắng",
)
c5.metric(
    "Edge Xác suất (Theo vốn)",
    f"{all_m['weighted_edge']:+.1f}%",
    f"Giá mua TB: {all_m['weighted_entry']:.1f}%",
)
hold_str = f"Giữ TB: {all_m['median_hold_h']:.1f}h" if pd.notna(all_m["median_hold_h"]) else "Giữ TB: N/A"
trades_str = f"{all_m['avg_trades']:.1f} lệnh/kèo" if pd.notna(all_m["avg_trades"]) else "N/A"
c6.metric(
    "Tần suất khớp lệnh",
    trades_str,
    hold_str,
)

st.divider()

# -------------------- PHẦN 2: SO SÁNH UNDERDOG VS FAVORITE --------------------
st.subheader(f"2️⃣ So sánh Kèo dưới {threshold_pct}% vs. Kèo trên {threshold_pct}%")
col_l, col_r = st.columns(2)


def render_side_panel(container, title_str: str, m: dict):
    with container:
        st.markdown(f"#### {title_str}")
        r1c1, r1c2, r1c3 = st.columns(3)
        r1c1.metric("Win Rate (Kèo)", f"{m['win_rate']:.1f}%", f"{m['wins']}/{m['n']} kèo")
        r1c2.metric("Win Rate (Theo vốn)", f"{m['cap_win_rate']:.1f}%", f"Giá mua TB: {m['weighted_entry']:.1f}%")
        r1c3.metric("Edge Thực tế (Vốn)", f"{m['weighted_edge']:+.1f}%", f"Edge số kèo: {m['simple_edge']:+.1f}%")

        r2c1, r2c2, r2c3 = st.columns(3)
        r2c1.metric("PnL Ròng", f"${m['pnl']:+,.2f}", f"ROI thực: {m['roi']:+.1f}%")
        r2c2.metric("PnL ex-Top 5", f"${m['pnl_ex_top5']:+,.2f}", f"Top 5: {m['top5_share']:.1f}%")
        pf_s = f"{m['profit_factor']:.2f}" if np.isfinite(m["profit_factor"]) else "∞"
        r2c3.metric("Tổng Vốn Mua", f"${m['cost']:,.2f}", f"Profit Factor: {pf_s}")


render_side_panel(col_l, f"🔵 Dưới {threshold_pct}% (Underdog)", under_m)
render_side_panel(col_r, f"🟠 Trên {threshold_pct}% (Favorite)", over_m)

st.divider()

# -------------------- PHẦN 3: PHÂN TÁCH 5 DẢI XÁC SUẤT & HÀNH VI --------------------
col_bucket, col_style = st.columns([1.3, 1.0])

with col_bucket:
    st.subheader("3️⃣ Phân rã Edge theo 5 dải xác suất")
    bins = [0.0, 0.2, 0.4, 0.6, 0.8, 1.01]
    labels = ["0% – 20%", "20% – 40%", "40% – 60%", "60% – 80%", "80% – 100%"]
    df["prob_band"] = pd.cut(df["avg_price"], bins=bins, labels=labels, right=False)

    band_rows = []
    for lbl in labels:
        sub_b = df[df["prob_band"] == lbl]
        bm = compute_edge_metrics(sub_b)
        band_rows.append({
            "Dải giá mua": lbl,
            "Số kèo": bm["n"],
            "Win Rate": bm["win_rate"],
            "Giá mua TB": bm["weighted_entry"],
            "Edge (Vốn)": bm["weighted_edge"],
            "Vốn thực ($)": bm["cost"],
            "PnL ($)": bm["pnl"],
            "ROI (%)": bm["roi"],
        })
    band_df = pd.DataFrame(band_rows)
    st.dataframe(
        band_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Win Rate": st.column_config.NumberColumn(format="%.1f%%"),
            "Giá mua TB": st.column_config.NumberColumn(format="%.1f%%"),
            "Edge (Vốn)": st.column_config.NumberColumn(format="%+.1f%%"),
            "Vốn thực ($)": st.column_config.NumberColumn(format="$%,.2f"),
            "PnL ($)": st.column_config.NumberColumn(format="$%+,.2f"),
            "ROI (%)": st.column_config.NumberColumn(format="%+.1f%%"),
        },
    )

with col_style:
    st.subheader("4️⃣ Nguồn gốc PnL theo Hành vi thoát lệnh")
    style_rows = []
    for style_name, sub_s in df.groupby("exit_style"):
        sm = compute_edge_metrics(sub_s)
        style_rows.append({
            "Hành vi": style_name,
            "Số kèo": sm["n"],
            "Win Rate": sm["win_rate"],
            "Lệnh/kèo": sm["avg_trades"],
            "Vốn ($)": sm["cost"],
            "PnL ($)": sm["pnl"],
            "ROI (%)": sm["roi"],
        })
    if style_rows:
        st.dataframe(
            pd.DataFrame(style_rows).sort_values("PnL ($)", ascending=False),
            use_container_width=True,
            hide_index=True,
            column_config={
                "Win Rate": st.column_config.NumberColumn(format="%.1f%%"),
                "Lệnh/kèo": st.column_config.NumberColumn(format="%.1f"),
                "Vốn ($)": st.column_config.NumberColumn(format="$%,.2f"),
                "PnL ($)": st.column_config.NumberColumn(format="$%+,.2f"),
                "ROI (%)": st.column_config.NumberColumn(format="%+.1f%%"),
            },
        )

st.divider()

# -------------------- PHẦN 4: BIỂU ĐỒ PNL TÍCH LŨY (SO SÁNH EX-TOP 5) --------------------
if df["time"].notna().any():
    st.subheader("5️⃣ Đường cong PnL tích lũy: Toàn bộ vs. Loại trừ Top 5 Outlier")
    chart_df = df.dropna(subset=["time"]).sort_values("time").copy()

    # Xác định Top 5 kèo thắng lớn nhất để vẽ đường đối chứng
    top5_indices = chart_df.loc[chart_df["total_pnl"] > 0].nlargest(5, "total_pnl").index
    chart_df["pnl_no_top5"] = chart_df["total_pnl"].where(~chart_df.index.isin(top5_indices), 0.0)
    chart_df["date"] = chart_df["time"].dt.date

    daily = (
        chart_df.groupby("date")[["total_pnl", "pnl_no_top5"]]
        .sum()
        .cumsum()
        .rename(columns={
            "total_pnl": "1. Tổng PnL tích lũy ($)",
            "pnl_no_top5": "2. PnL tích lũy (Bỏ Top 5 kèo thắng lớn nhất)",
        })
    )
    st.line_chart(daily)

# -------------------- PHẦN 5: BẢNG DỮ LIỆU CHI TIẾT --------------------
st.subheader("6️⃣ Bảng kiểm toán chi tiết từng vị thế")
tab_under, tab_over, tab_all = st.tabs([
    f"Dưới {threshold_pct}% ({len(under_df)})",
    f"Trên {threshold_pct}% ({len(over_df)})",
    f"Tất cả ({len(df)})",
])


def render_detail_table(target_df: pd.DataFrame, suffix: str):
    if target_df.empty:
        st.write("Không có dữ liệu.")
        return

    cols = [
        "time", "status", "exit_style", "title", "outcome",
        "avg_price", "true_cost", "total_pnl", "true_roi_pct",
        "total_trades", "hold_hours", "cost_source", "url",
    ]
    disp = target_df[cols].sort_values("total_pnl", ascending=False).copy()
    disp["avg_price"] = disp["avg_price"] * 100.0

    st.dataframe(
        disp,
        use_container_width=True,
        hide_index=True,
        column_config={
            "time": st.column_config.DatetimeColumn("Thời gian", format="DD/MM/YYYY HH:mm"),
            "status": st.column_config.TextColumn("Status"),
            "exit_style": st.column_config.TextColumn("Hành vi"),
            "title": st.column_config.TextColumn("Market", width="large"),
            "outcome": st.column_config.TextColumn("Cửa"),
            "avg_price": st.column_config.NumberColumn("Giá mua TB", format="%.1f%%"),
            "true_cost": st.column_config.NumberColumn("Vốn thật ($)", format="$%,.2f"),
            "total_pnl": st.column_config.NumberColumn("Tổng PnL ($)", format="$%+,.2f"),
            "true_roi_pct": st.column_config.NumberColumn("ROI thật", format="%+.1f%%"),
            "total_trades": st.column_config.NumberColumn("Số lệnh", format="%d"),
            "hold_hours": st.column_config.NumberColumn("Giữ (giờ)", format="%.1f"),
            "cost_source": st.column_config.TextColumn("Nguồn vốn"),
            "url": st.column_config.LinkColumn("Link", display_text="Xem"),
        },
    )

    csv_bytes = target_df.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        label="Tải toàn bộ dữ liệu kiểm toán (.CSV)",
        data=csv_bytes,
        file_name=f"polymarket_audit_{suffix}.csv",
        mime="text/csv",
        key=f"dl_audit_{suffix}",
    )


with tab_under:
    render_detail_table(under_df, "under")
with tab_over:
    render_detail_table(over_df, "over")
with tab_all:
    render_detail_table(df, "all")
