import streamlit as st
import requests
import pandas as pd

st.set_page_config(
    page_title="Polymarket Wallet Analyzer",
    page_icon="📊",
    layout="wide"
)

DATA_API_URL = "https://data-api.polymarket.com/closed-positions"


@st.cache_data(ttl=300, show_spinner="Đang kéo lịch sử vị thế từ Polymarket...")
def fetch_closed_positions(wallet: str) -> pd.DataFrame:
    all_positions = []
    offset = 0
    limit = 50

    while True:
        params = {
            "user": wallet.strip(),
            "limit": limit,
            "offset": offset,
            "sortBy": "TIMESTAMP",
            "sortDirection": "DESC",
        }
        res = requests.get(DATA_API_URL, params=params, timeout=15)
        res.raise_for_status()
        data = res.json()

        if not data:
            break
        all_positions.extend(data)
        if len(data) < limit:
            break
        offset += limit

    if not all_positions:
        return pd.DataFrame()

    df = pd.DataFrame(all_positions)

    # Chuẩn hóa kiểu dữ liệu số
    numeric_cols = ["avgPrice", "totalBought", "realizedPnl", "curPrice"]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df.get(col, 0), errors="coerce").fillna(0.0)

    df["cost"] = df["avgPrice"] * df["totalBought"]
    df["roi_pct"] = df.apply(
        lambda r: (r["realizedPnl"] / r["cost"] * 100) if r["cost"] > 0 else 0.0,
        axis=1,
    )
    df["is_win"] = df["realizedPnl"] > 0

    # Phân loại kèo giữ đến đáo hạn (giá cuối ~0 hoặc ~1) vs chốt/cắt sớm
    df["is_resolved"] = (df["curPrice"] <= 0.001) | (df["curPrice"] >= 0.999)

    if "timestamp" in df.columns:
        df["time"] = pd.to_datetime(df["timestamp"], unit="s", errors="coerce")
    else:
        df["time"] = pd.NaT

    if "eventSlug" in df.columns:
        df["url"] = "https://polymarket.com/event/" + df["eventSlug"].astype(str)
    else:
        df["url"] = ""

    return df


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
        help="Lấy địa chỉ ví 0x... tại trang cá nhân của trader trên Polymarket.",
    )

    threshold_pct = st.slider(
        "Ngưỡng chia xác suất (%)",
        min_value=5,
        max_value=95,
        value=50,
        step=5,
        help="Mặc định 50%: tách kèo mua dưới 50% và trên 50%.",
    )

    trade_type = st.radio(
        "Loại lệnh tính thống kê",
        options=["Tất cả vị thế đã đóng", "Chỉ kèo giữ đến đáo hạn", "Chỉ kèo lướt sóng (đóng sớm)"],
        index=0,
    )

    min_cost = st.number_input("Lọc vốn tối thiểu mỗi lệnh ($)", min_value=0.0, value=0.0, step=10.0)

    if st.button("Làm mới dữ liệu", use_container_width=True):
        st.cache_data.clear()

# --- MAIN UI ---
st.title("📊 Phân tích hiệu suất ví Polymarket")

if not wallet_input:
    st.info("Nhập địa chỉ ví `0x...` ở thanh bên trái để bắt đầu thống kê.")
    st.stop()

try:
    raw_df = fetch_closed_positions(wallet_input)
except Exception as e:
    st.error(f"Lỗi khi gọi API Polymarket: {e}")
    st.stop()

if raw_df.empty:
    st.warning("Không tìm thấy vị thế đã đóng nào cho địa chỉ ví này.")
    st.stop()

# Áp dụng bộ lọc
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

# Chia 2 nhóm theo ngưỡng
thresh = threshold_pct / 100.0
under_df = df[df["avgPrice"] < thresh].copy()
over_df = df[df["avgPrice"] > thresh].copy()
exact_df = df[df["avgPrice"] == thresh].copy()

under_stats = summarize_bucket(under_df)
over_stats = summarize_bucket(over_df)

# --- TỔNG QUAN SO SÁNH ---
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

# --- BIỂU ĐỒ PNL TÍCH LŨY ---
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

# --- BẢNG CHI TIẾT ---
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
    render_table(df, "all")
