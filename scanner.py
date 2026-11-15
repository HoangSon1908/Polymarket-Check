import streamlit as st
import requests
import pandas as pd

st.set_page_config(
    page_title="Polymarket Wallet Analyzer",
    page_icon="📊",
    layout="wide",
)

CANDIDATE_ENDPOINTS = [
    ("v2/positions (all)", "https://data-api.polymarket.com/v2/positions", {"sizeThreshold": 0}),
    ("v2/positions (closed)", "https://data-api.polymarket.com/v2/positions", {"closed": "true"}),
    ("v2/activity", "https://data-api.polymarket.com/v2/activity", {}),
    ("v2/trades", "https://data-api.polymarket.com/v2/trades", {}),
]


def pick_series(df: pd.DataFrame, candidates: list[str], default=0.0) -> pd.Series:
    for col in candidates:
        if col in df.columns:
            return df[col]
    return pd.Series([default] * len(df), index=df.index)


def fetch_paginated(url: str, wallet: str, extra_params: dict) -> list[dict]:
    items = []
    offset = 0
    limit = 100
    cursor = None

    while True:
        params = {"user": wallet.strip(), "limit": limit, **extra_params}
        if cursor:
            params["cursor"] = cursor
        else:
            params["offset"] = offset

        res = requests.get(url, params=params, timeout=15)
        res.raise_for_status()
        payload = res.json()

        next_cursor = None
        if isinstance(payload, list):
            batch = payload
        elif isinstance(payload, dict):
            batch = (
                payload.get("data")
                or payload.get("positions")
                or payload.get("activities")
                or payload.get("trades")
                or payload.get("results")
                or []
            )
            next_cursor = payload.get("next_cursor") or payload.get("nextCursor")
        else:
            break

        if not batch:
            break

        items.extend(batch)

        if next_cursor and next_cursor != "LTE=":
            cursor = next_cursor
        else:
            if len(batch) < limit or offset >= 5000:
                break
            offset += limit

    return items


def normalize_from_positions(raw_items: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(raw_items)

    df["avgPrice"] = pd.to_numeric(
        pick_series(df, ["avgPrice", "avg_price", "averagePrice"]), errors="coerce"
    ).fillna(0.0)
    df["totalBought"] = pd.to_numeric(
        pick_series(df, ["totalBought", "total_bought", "size", "shares"]), errors="coerce"
    ).fillna(0.0)
    df["size"] = pd.to_numeric(
        pick_series(df, ["size", "remainingSize", "shares"]), errors="coerce"
    ).fillna(0.0)
    df["curPrice"] = pd.to_numeric(
        pick_series(df, ["curPrice", "cur_price", "currentPrice", "closePrice"]), errors="coerce"
    ).fillna(0.0)
    raw_realized = pd.to_numeric(
        pick_series(df, ["realizedPnl", "realized_pnl", "cashPnl", "pnl"]), errors="coerce"
    ).fillna(0.0)

    redeemable = pick_series(df, ["redeemable"], default=False).fillna(False).astype(bool)
    is_settled = (df["curPrice"] <= 0.001) | (df["curPrice"] >= 0.999) | redeemable
    is_closed = (df["size"] <= 0.001) | is_settled

    # Giữ lại các vị thế đã đóng hoặc đã ngã ngũ (kể cả kèo thua/thắng chưa bấm claim)
    df = df[is_closed].copy()
    if df.empty:
        return pd.DataFrame()

    raw_realized = raw_realized.loc[df.index]
    unclaimed_pnl = (df["curPrice"] - df["avgPrice"]) * df["size"]
    df["realizedPnl"] = raw_realized.where(raw_realized.abs() > 1e-6, unclaimed_pnl)

    # Nếu totalBought bị trả về 0 ở vị thế đã ngã ngũ, fallback bằng size
    df["totalBought"] = df["totalBought"].where(df["totalBought"] > 0, df["size"])
    df["cost"] = df["avgPrice"] * df["totalBought"]
    df["is_resolved"] = (df["curPrice"] <= 0.001) | (df["curPrice"] >= 0.999) | redeemable.loc[df.index]

    df["title"] = pick_series(df, ["title", "question", "market"], default="N/A").astype(str)
    df["outcome"] = pick_series(df, ["outcome", "side"], default="").astype(str)

    raw_time = pick_series(df, ["timestamp", "closedAt", "updatedAt", "endDate"], default=None)
    if pd.api.types.is_numeric_dtype(raw_time):
        df["time"] = pd.to_datetime(raw_time, unit="s", errors="coerce")
    else:
        df["time"] = pd.to_datetime(raw_time, errors="coerce")

    slug_series = pick_series(df, ["eventSlug", "event_slug", "slug"], default="")
    df["url"] = slug_series.apply(
        lambda s: f"https://polymarket.com/event/{s}" if pd.notna(s) and str(s).strip() else ""
    )
    return df


def normalize_from_events(raw_items: list[dict]) -> pd.DataFrame:
    """Tổng hợp lại vị thế từ lịch sử activity/trades nếu endpoint positions không khả dụng."""
    ev = pd.DataFrame(raw_items)
    ev["conditionId"] = pick_series(ev, ["conditionId", "condition_id", "market", "asset"], default="").astype(str)
    ev["outcome"] = pick_series(ev, ["outcome", "outcomeIndex"], default="").astype(str)
    ev["side"] = pick_series(ev, ["side"], default="BUY").astype(str).str.upper()
    ev["type"] = pick_series(ev, ["type"], default="TRADE").astype(str).str.upper()
    ev["price"] = pd.to_numeric(pick_series(ev, ["price"]), errors="coerce").fillna(0.0)
    ev["size"] = pd.to_numeric(pick_series(ev, ["size", "shares", "amount"]), errors="coerce").fillna(0.0)
    ev["usdcSize"] = pd.to_numeric(pick_series(ev, ["usdcSize", "usdc_size"]), errors="coerce").fillna(0.0)
    ev["title"] = pick_series(ev, ["title", "question"], default="N/A").astype(str)
    ev["slug"] = pick_series(ev, ["eventSlug", "event_slug", "slug"], default="").astype(str)
    ev["timestamp"] = pd.to_numeric(pick_series(ev, ["timestamp"]), errors="coerce")

    rows = []
    for (cid, outcome), grp in ev.groupby(["conditionId", "outcome"]):
        if not cid:
            continue
        buys = grp[(grp["type"] == "TRADE") & (grp["side"] == "BUY")]
        sells = grp[(grp["type"] == "TRADE") & (grp["side"] == "SELL")]
        redeems = grp[grp["type"] == "REDEEM"]

        buy_shares = buys["size"].sum()
        if buy_shares <= 0:
            continue

        buy_cost = buys["usdcSize"].where(buys["usdcSize"] > 0, buys["price"] * buys["size"]).sum()
        sell_shares = sells["size"].sum()
        sell_proceeds = sells["usdcSize"].where(sells["usdcSize"] > 0, sells["price"] * sells["size"]).sum()
        redeem_proceeds = redeems["usdcSize"].where(redeems["usdcSize"] > 0, redeems["size"]).sum()

        avg_price = buy_cost / buy_shares
        is_redeemed = redeem_proceeds > 0
        is_sold_out = sell_shares >= buy_shares * 0.98

        # Chỉ lấy các vị thế đã bán hết hoặc đã redeem
        if not (is_redeemed or is_sold_out):
            continue

        realized_pnl = sell_proceeds + redeem_proceeds - buy_cost
        cur_price = 1.0 if is_redeemed else (sell_proceeds / sell_shares if sell_shares > 0 else 0.0)
        last_ts = grp["timestamp"].max()
        slug = grp["slug"].iloc[0]

        rows.append({
            "time": pd.to_datetime(last_ts, unit="s", errors="coerce") if pd.notna(last_ts) else pd.NaT,
            "title": grp["title"].iloc[0],
            "outcome": outcome,
            "avgPrice": avg_price,
            "curPrice": cur_price,
            "totalBought": buy_shares,
            "cost": buy_cost,
            "realizedPnl": realized_pnl,
            "is_resolved": is_redeemed,
            "url": f"https://polymarket.com/event/{slug}" if slug else "",
        })

    return pd.DataFrame(rows)


@st.cache_data(ttl=300, show_spinner="Đang kéo dữ liệu từ Polymarket API...")
def load_wallet_data(wallet: str, custom_url: str = "") -> tuple[pd.DataFrame, str, dict]:
    errors = []
    endpoints_to_try = (
        [("Custom URL", custom_url.strip(), {})] if custom_url.strip() else CANDIDATE_ENDPOINTS
    )

    for label, url, extra_params in endpoints_to_try:
        try:
            raw_items = fetch_paginated(url, wallet, extra_params)
            if not raw_items:
                errors.append(f"{label}: 200 OK nhưng rỗng")
                continue

            sample = raw_items[0]
            # Phân biệt dữ liệu dạng vị thế (positions) hay dạng lịch sử lệnh (activity/trades)
            if any(k in sample for k in ["avgPrice", "avg_price", "realizedPnl", "realized_pnl", "curPrice"]):
                df = normalize_from_positions(raw_items)
            else:
                df = normalize_from_events(raw_items)

            if df.empty:
                errors.append(f"{label}: có dữ liệu nhưng không lọc được vị thế đã đóng")
                continue

            df["roi_pct"] = df.apply(
                lambda r: (r["realizedPnl"] / r["cost"] * 100) if r["cost"] > 0 else 0.0,
                axis=1,
            )
            df["is_win"] = df["realizedPnl"] > 0
            return df, f"{label} ({url})", sample

        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else "ERR"
            errors.append(f"{label} -> HTTP {code}")
        except Exception as e:
            errors.append(f"{label} -> {e}")

    raise RuntimeError(" | ".join(errors))


def summarize_bucket(sub_df: pd.DataFrame) -> dict:
    total = len(sub_df)
    if total == 0:
        return {"total": 0, "wins": 0, "win_rate": 0.0, "cost": 0.0, "pnl": 0.0, "roi": 0.0}
    wins = int(sub_df["is_win"].sum())
    cost = float(sub_df["cost"].sum())
    pnl = float(sub_df["realizedPnl"].sum())
    return {
        "total": total,
        "wins": wins,
        "win_rate": (wins / total) * 100,
        "cost": cost,
        "pnl": pnl,
        "roi": (pnl / cost * 100) if cost > 0 else 0.0,
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

    trade_type = st.radio(
        "Loại lệnh tính thống kê",
        options=["Tất cả vị thế đã đóng", "Chỉ kèo giữ đến đáo hạn", "Chỉ kèo lướt sóng (đóng sớm)"],
        index=0,
    )

    min_cost = st.number_input("Lọc vốn tối thiểu mỗi lệnh ($)", min_value=0.0, value=0.0, step=10.0)

    with st.expander("Tùy chỉnh API Endpoint (nếu cần)"):
        custom_endpoint = st.text_input(
            "URL Endpoint v2",
            placeholder="Để trống để tự động dò",
            help="Ví dụ: https://data-api.polymarket.com/v2/positions",
        )

    if st.button("Làm mới dữ liệu", use_container_width=True):
        st.cache_data.clear()

# --- MAIN UI ---
st.title("📊 Phân tích hiệu suất ví Polymarket")

if not wallet_input:
    st.info("Nhập địa chỉ ví `0x...` ở thanh bên trái để bắt đầu thống kê.")
    st.stop()

try:
    raw_df, used_source, sample_json = load_wallet_data(wallet_input, custom_endpoint)
    st.caption(f"Nguồn dữ liệu đang dùng: `{used_source}`")
except Exception as e:
    st.error(f"Không lấy được dữ liệu từ các endpoint v2: {e}")
    st.info("Bạn có thể mở `https://docs.polymarket.com/api-reference/data-api/overview` để lấy đúng đường dẫn mới và dán vào ô **Tùy chỉnh API Endpoint** ở thanh bên trái.")
    st.stop()

df = raw_df.copy()
if trade_type == "Chỉ kèo giữ đến đáo hạn":
    df = df[df["is_resolved"]]
elif trade_type == "Chỉ kèo lướt sóng (đóng sớm)":
    df = df[~df["is_resolved"]]

if min_cost > 0:
    df = df[df["cost"] >= min_cost]

if df.empty:
    st.warning("Không có lệnh nào khớp với bộ lọc hiện tại.")
    st.stop()

thresh = threshold_pct / 100.0
under_df = df[df["avgPrice"] < thresh].copy()
over_df = df[df["avgPrice"] > thresh].copy()
exact_df = df[df["avgPrice"] == thresh].copy()

under_stats = summarize_bucket(under_df)
over_stats = summarize_bucket(over_df)

col_left, col_right = st.columns(2)

with col_left:
    st.subheader(f"🔵 Kèo dưới {threshold_pct}% (Underdog)")
    m1, m2, m3 = st.columns(3)
    m1.metric("Tỷ lệ thắng", f"{under_stats['win_rate']:.1f}%", f"{under_stats['wins']}/{under_stats['total']} kèo")
    m2.metric("Lời / Lỗ ròng", f"${under_stats['pnl']:+,.2f}", f"ROI: {under_stats['roi']:+.1f}%")
    m3.metric("Tổng vốn vào", f"${under_stats['cost']:,.2f}")

with col_right:
    st.subheader(f"🟠 Kèo trên {threshold_pct}% (Favorite)")
    m1, m2, m3 = st.columns(3)
    m1.metric("Tỷ lệ thắng", f"{over_stats['win_rate']:.1f}%", f"{over_stats['wins']}/{over_stats['total']} kèo")
    m2.metric("Lời / Lỗ ròng", f"${over_stats['pnl']:+,.2f}", f"ROI: {over_stats['roi']:+.1f}%")
    m3.metric("Tổng vốn vào", f"${over_stats['cost']:,.2f}")

if len(exact_df) > 0:
    st.caption(f"Lưu ý: Có {len(exact_df)} vị thế khớp đúng mức {threshold_pct}% không nằm trong 2 cột trên.")

st.divider()

if df["time"].notna().any():
    st.subheader("📈 Biểu đồ PnL tích lũy theo ngày")
    chart_df = df.dropna(subset=["time"]).copy()
    chart_df["date"] = chart_df["time"].dt.date
    chart_df["Nhóm"] = chart_df["avgPrice"].apply(
        lambda p: f"Dưới {threshold_pct}%" if p < thresh else (f"Trên {threshold_pct}%" if p > thresh else f"Đúng {threshold_pct}%")
    )
    daily_pnl = (
        chart_df.pivot_table(index="date", columns="Nhóm", values="realizedPnl", aggfunc="sum")
        .fillna(0)
        .cumsum()
    )
    st.line_chart(daily_pnl)

st.subheader("📋 Chi tiết từng vị thế đã đóng")
tab_under, tab_over, tab_all = st.tabs([
    f"Dưới {threshold_pct}% ({len(under_df)})",
    f"Trên {threshold_pct}% ({len(over_df)})",
    f"Tất cả ({len(df)})",
])


def render_table(target_df: pd.DataFrame, key_suffix: str):
    if target_df.empty:
        st.write("Không có dữ liệu.")
        return

    display_df = target_df[["time", "title", "outcome", "avgPrice", "curPrice", "cost", "realizedPnl", "roi_pct", "is_resolved", "url"]].copy()
    display_df["avgPrice"] = display_df["avgPrice"] * 100
    display_df["curPrice"] = display_df["curPrice"] * 100

    st.dataframe(
        display_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "time": st.column_config.DatetimeColumn("Thời gian", format="DD/MM/YYYY HH:mm"),
            "title": st.column_config.TextColumn("Market", width="large"),
            "outcome": st.column_config.TextColumn("Cửa"),
            "avgPrice": st.column_config.NumberColumn("Giá mua TB", format="%.1f%%"),
            "curPrice": st.column_config.NumberColumn("Giá đóng", format="%.1f%%"),
            "cost": st.column_config.NumberColumn("Vốn ($)", format="$%.2f"),
            "realizedPnl": st.column_config.NumberColumn("PnL ($)", format="$%+.2f"),
            "roi_pct": st.column_config.NumberColumn("ROI", format="%+.1f%%"),
            "is_resolved": st.column_config.CheckboxColumn("Đáo hạn"),
            "url": st.column_config.LinkColumn("Link", display_text="Xem"),
        },
    )

    csv = target_df.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        label="Tải xuống CSV",
        data=csv,
        file_name=f"polymarket_{key_suffix}.csv",
        mime="text/csv",
        key=f"dl_{key_suffix}",
    )


with tab_under:
    render_table(under_df, "under")
with tab_over:
    render_table(over_df, "over")
with tab_all:
    render_table(df, "
