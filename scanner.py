import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import requests
import streamlit as st

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

# Danh sách Tag chuẩn theo giao diện Polymarket Leaderboard
POLYMARKET_TAGS = {
    "All Categories": None,
    "Politics": "politics",
    "Sports": "sports",
    "Esports": "esports",
    "Crypto": "crypto",
    "Finance": "finance",
    "Culture": "culture",
    "Mentions": "mentions",
    "Weather": "weather",
    "Economics": "economics",
    "Tech": "tech",
}

st.set_page_config(
    page_title="Polymarket Copy-Wallet Scanner",
    page_icon="🎯",
    layout="wide"
)

# --- HÀM GỌI API POLYMARKET ---
def fetch_candidate_wallets(tag_slug: str | None, sub_keyword: str, market_limit: int, trades_per_market: int, progress_bar=None):
    """Lấy danh sách thị trường theo Tag của Polymarket và trích xuất proxyWallet."""
    params = {
        "active": "true",
        "closed": "false",
        "limit": 100,
        "order": "volume24hr",
        "ascending": "false",
    }
    if tag_slug:
        params["tag_slug"] = tag_slug

    try:
        resp = requests.get(f"{GAMMA_API}/events", params=params, timeout=15)
        resp.raise_for_status()
        events = resp.json()
    except Exception:
        return []

    # Gom toàn bộ market (conditionId) nằm trong các event thuộc tag
    markets = []
    kw = sub_keyword.strip().lower()
    for ev in events:
        ev_title = ev.get("title", "").lower()
        for m in ev.get("markets", []):
            if m.get("closed") or not m.get("conditionId"):
                continue
            q_text = m.get("question", "").lower()
            if not kw or kw in q_text or kw in ev_title:
                markets.append(m)

    target_markets = markets[:market_limit]
    if not target_markets:
        return []

    wallets = set()
    for idx, m in enumerate(target_markets):
        cid = m.get("conditionId")
        try:
            t_resp = requests.get(
                f"{DATA_API}/trades",
                params={"market": cid, "limit": trades_per_market},
                timeout=12
            )
            if t_resp.status_code == 200:
                for trade in t_resp.json():
                    w = trade.get("proxyWallet")
                    if w:
                        wallets.add(w.lower())
        except Exception:
            pass

        if progress_bar:
            progress_bar.progress(
                (idx + 1) / len(target_markets),
                text=f"Đang quét kèo {idx + 1}/{len(target_markets)} — Đã gom {len(wallets)} ví..."
            )
        time.sleep(0.08)

    return list(wallets)


def fetch_single_profile(wallet: str, time_period: str = "ALL"):
    """Lấy chỉ số PnL, Volume và Markets Traded của 1 địa chỉ ví."""
    wallet = wallet.strip().lower()
    try:
        lb_resp = requests.get(
            f"{DATA_API}/v1/leaderboard",
            params={"user": wallet, "timePeriod": time_period, "category": "OVERALL"},
            timeout=10
        )
        lb_data = lb_resp.json() if lb_resp.status_code == 200 else []
        if not lb_data:
            return None

        user_stats = lb_data[0]
        pnl = float(user_stats.get("pnl", 0))
        vol = float(user_stats.get("vol", 0))
        username = user_stats.get("userName") or f"{wallet[:6]}...{wallet[-4:]}"

        if vol <= 0:
            return None

        tr_resp = requests.get(
            f"{DATA_API}/traded",
            params={"user": wallet},
            timeout=10
        )
        markets_traded = int(tr_resp.json().get("traded", 0)) if tr_resp.status_code == 200 else 0

        return {
            "Username": username,
            "Wallet": wallet,
            "Profile": f"https://polymarket.com/{wallet}",
            "PnL ($)": round(pnl, 2),
            "Volume ($)": round(vol, 2),
            "PnL / Volume (%)": round((pnl / vol) * 100, 3),
            "Markets Traded": markets_traded,
        }
    except Exception:
        return None


def scan_profiles_concurrent(wallets: list, time_period: str, progress_bar):
    """Phân tích song song danh sách ví để tối ưu tốc độ."""
    results = []
    total = len(wallets)
    if total == 0:
        return pd.DataFrame()

    completed = 0
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_wallet = {
            executor.submit(fetch_single_profile, w, time_period): w for w in wallets
        }
        for future in as_completed(future_to_wallet):
            completed += 1
            data = future.result()
            if data:
                results.append(data)
            if completed % 5 == 0 or completed == total:
                progress_bar.progress(
                    completed / total,
                    text=f"Đang phân tích hồ sơ ví: {completed}/{total}..."
                )

    return pd.DataFrame(results)


# --- GIAO DIỆN SIDEBAR: ĐIỀU CHỈNH THÔNG SỐ ---
with st.sidebar:
    st.header("⚙️ Cấu hình bộ lọc")

    st.subheader("1. Tiêu chí PnL / Volume")
    col_r1, col_r2 = st.columns(2)
    with col_r1:
        min_ratio = st.number_input(
            "Min PnL/Vol (%)",
            min_value=-100.0,
            max_value=100.0,
            value=0.15,
            step=0.05,
            format="%.2f"
        )
    with col_r2:
        max_ratio = st.number_input(
            "Max PnL/Vol (%)",
            min_value=0.0,
            max_value=500.0,
            value=3.50,
            step=0.10,
            format="%.2f"
        )

    min_pnl = st.number_input(
        "Tổng PnL tối thiểu ($)",
        min_value=0.0,
        max_value=1000000.0,
        value=100.0,
        step=50.0
    )

    st.divider()
    st.subheader("2. Số lượng thị trường đã đánh")
    col_m1, col_m2 = st.columns(2)
    with col_m1:
        min_markets = st.number_input(
            "Min Markets (Chống ví mới)",
            min_value=1,
            max_value=50000,
            value=200,
            step=50
        )
    with col_m2:
        max_markets = st.number_input(
            "Max Markets (Chống bot)",
            min_value=10,
            max_value=100000,
            value=4500,
            step=100
        )

    st.divider()
    st.subheader("3. Phạm vi quét dữ liệu")
    selected_tag_label = st.selectbox(
        "Category Tag (Polymarket)",
        options=list(POLYMARKET_TAGS.keys()),
        index=8  # Mặc định chọn sẵn "Weather"
    )
    sub_keyword = st.text_input(
        "Từ khóa lọc sâu (tùy chọn)",
        value="",
        placeholder="Để trống để quét toàn bộ tag đã chọn..."
    )
    time_period = st.selectbox(
        "Khung thời gian tính PnL/Vol",
        options=["ALL", "MONTH", "WEEK"],
        index=0
    )
    market_limit = st.slider("Số thị trường quét", min_value=5, max_value=100, value=25, step=5)
    trades_per_market = st.slider("Số giao dịch/thị trường", min_value=50, max_value=500, value=100, step=50)


# --- GIAO DIỆN CHÍNH ---
st.title("🎯 Polymarket Copy-Wallet Filter")
st.caption("Lọc ví theo hiệu suất PnL/Volume và số lượng thị trường đã tham gia theo từng Category Tag.")

tab_scan, tab_single = st.tabs(["🔍 Quét ví theo Tag", "🧪 Kiểm tra nhanh 1 ví"])

with tab_scan:
    col_btn, col_info = st.columns([1, 3])
    with col_btn:
        start_scan = st.button(f"🚀 Quét tag {selected_tag_label}", type="primary", use_container_width=True)
    with col_info:
        if "raw_df" in st.session_state and not st.session_state["raw_df"].empty:
            st.info(f"Đang lưu sẵn **{len(st.session_state['raw_df'])}** hồ sơ ví trong bộ nhớ. Kéo chỉnh thông số bên trái để lọc lại ngay lập tức.")

    if start_scan:
        progress = st.progress(0.0, text=f"Đang tải danh sách kèo thuộc tag {selected_tag_label}...")
        wallets = fetch_candidate_wallets(
            tag_slug=POLYMARKET_TAGS[selected_tag_label],
            sub_keyword=sub_keyword,
            market_limit=market_limit,
            trades_per_market=trades_per_market,
            progress_bar=progress
        )

        if not wallets:
            progress.empty()
            st.warning("Không tìm thấy giao dịch nào trong tag này. Hãy thử xóa từ khóa lọc sâu hoặc chọn tag khác.")
        else:
            raw_df = scan_profiles_concurrent(wallets, time_period=time_period, progress_bar=progress)
            progress.empty()
            st.session_state["raw_df"] = raw_df

    if "raw_df" in st.session_state and not st.session_state["raw_df"].empty:
        df = st.session_state["raw_df"].copy()

        filtered_df = df[
            (df["PnL ($)"] >= min_pnl) &
            (df["PnL / Volume (%)"] >= min_ratio) &
            (df["PnL / Volume (%)"] <= max_ratio) &
            (df["Markets Traded"] >= min_markets) &
            (df["Markets Traded"] <= max_markets)
        ].sort_values(by="PnL / Volume (%)", ascending=False)

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Tổng ví đã quét", len(df))
        m2.metric("Ví đạt chuẩn lọc", len(filtered_df))
        avg_ratio = round(filtered_df["PnL / Volume (%)"].mean(), 3) if not filtered_df.empty else 0.0
        m3.metric("PnL/Vol TB nhóm đạt", f"{avg_ratio}%")
        avg_mkts = int(filtered_df["Markets Traded"].mean()) if not filtered_df.empty else 0
        m4.metric("Markets Traded TB", f"{avg_mkts:,}")

        if filtered_df.empty:
            st.warning("Chưa có ví nào khớp ngưỡng hiện tại. Hãy nới rộng khoảng PnL/Vol hoặc Markets Traded ở thanh bên trái.")
        else:
            st.dataframe(
                filtered_df,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Profile": st.column_config.LinkColumn(
                        "Polymarket Profile",
                        display_text="Mở hồ sơ ↗"
                    ),
                    "PnL ($)": st.column_config.NumberColumn(format="$%.2f"),
                    "Volume ($)": st.column_config.NumberColumn(format="$%.2f"),
                    "PnL / Volume (%)": st.column_config.NumberColumn(format="%.3f%%"),
                    "Markets Traded": st.column_config.NumberColumn(format="%d"),
                }
            )

            csv_bytes = filtered_df.to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                label="📥 Tải danh sách ví đạt chuẩn (.CSV)",
                data=csv_bytes,
                file_name=f"polymarket_{selected_tag_label.lower().replace(' ', '_')}_wallets.csv",
                mime="text/csv"
            )

with tab_single:
    st.write("Dán địa chỉ ví (`0x...`) bất kỳ để kiểm tra xem ví đó có vượt qua bộ lọc hiện tại không:")
    wallet_input = st.text_input("Địa chỉ Proxy Wallet", placeholder="0x1234abcd...")
    if st.button("Kiểm tra ví này") and wallet_input.strip():
        match = re.search(r"0x[a-fA-F0-9]{40}", wallet_input.strip())
        if not match:
            st.error("Địa chỉ ví không hợp lệ. Cần nhập chuỗi dạng 0x... (42 ký tự).")
        else:
            with st.spinner("Đang tải dữ liệu ví..."):
                stats = fetch_single_profile(match.group(0), time_period=time_period)
            if not stats:
                st.warning("Không tìm thấy dữ liệu giao dịch cho ví này.")
            else:
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Total PnL", f"${stats['PnL ($)']:,.2f}")
                c2.metric("Total Volume", f"${stats['Volume ($)']:,.2f}")
                c3.metric("PnL / Volume", f"{stats['PnL / Volume (%)']}%")
                c4.metric("Markets Traded", f"{stats['Markets Traded']:,}")

                pass_pnl = stats["PnL ($)"] >= min_pnl
                pass_ratio = min_ratio <= stats["PnL / Volume (%)"] <= max_ratio
                pass_mkts = min_markets <= stats["Markets Traded"] <= max_markets

                if pass_pnl and pass_ratio and pass_mkts:
                    st.success(f"✅ Ví **{stats['Username']}** ĐẠT CHUẨN tất cả tiêu chí hiện tại! ([Xem profile]({stats['Profile']}))")
                else:
                    reasons = []
                    if not pass_pnl:
                        reasons.append(f"PnL (${stats['PnL ($)']}) < Min PnL (${min_pnl})")
                    if not pass_ratio:
                        reasons.append(f"PnL/Vol ({stats['PnL / Volume (%)']}%) nằm ngoài khoảng [{min_ratio}% – {max_ratio}%]")
                    if not pass_mkts:
                        reasons.append(f"Markets ({stats['Markets Traded']}) nằm ngoài khoảng [{min_markets} – {max_markets}]")
                    st.error("❌ Ví chưa đạt chuẩn vì: " + " | ".join(reasons))
