import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import requests
import streamlit as st

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

POLYMARKET_TAGS = {
    "All Categories": {"slug": None, "lb_cat": "OVERALL"},
    "Politics": {"slug": "politics", "lb_cat": "POLITICS"},
    "Sports": {"slug": "sports", "lb_cat": "SPORTS"},
    "Esports": {"slug": "esports", "lb_cat": "ESPORTS"},
    "Crypto": {"slug": "crypto", "lb_cat": "CRYPTO"},
    "Finance": {"slug": "finance", "lb_cat": "FINANCE"},
    "Culture": {"slug": "culture", "lb_cat": "CULTURE"},
    "Mentions": {"slug": "mentions", "lb_cat": "MENTIONS"},
    "Weather": {"slug": "weather", "lb_cat": "WEATHER"},
    "Economics": {"slug": "economics", "lb_cat": "ECONOMICS"},
    "Tech": {"slug": "tech", "lb_cat": "TECH"},
}

st.set_page_config(
    page_title="Polymarket Copy-Wallet Scanner",
    page_icon="🎯",
    layout="wide"
)


# --- HÀM HỖ TRỢ GỌI DATA API V2 ---
def request_data_api(path: str, params: dict, timeout: int = 12):
    """Ưu tiên gọi Data API /v2/, tự động thử đường dẫn gốc nếu endpoint chưa chuyển sang v2."""
    clean_path = path.lstrip("/")
    urls = [f"{DATA_API}/v2/{clean_path}", f"{DATA_API}/{clean_path}"]
    last_resp = None
    for url in urls:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
        last_resp = resp
        if resp.status_code == 200:
            return resp
        if resp.status_code not in (404, 410):
            return resp
    return last_resp


def parse_leaderboard_row(row: dict) -> dict | None:
    """Trích xuất linh hoạt dữ liệu từ phản hồi Leaderboard v2."""
    wallet = (
        row.get("proxyWallet")
        or row.get("address")
        or row.get("user")
        or row.get("wallet")
        or ""
    ).strip().lower()
    if not wallet:
        return None

    raw_pnl = row.get("pnl") if row.get("pnl") is not None else row.get("profit", 0)
    raw_vol = row.get("vol") if row.get("vol") is not None else row.get("volume", 0)

    try:
        pnl = float(raw_pnl or 0)
        vol = float(raw_vol or 0)
    except (ValueError, TypeError):
        return None

    if vol <= 0:
        return None

    username = (
        row.get("userName")
        or row.get("username")
        or row.get("name")
        or f"{wallet[:6]}...{wallet[-4:]}"
    )

    return {
        "Username": username,
        "Wallet": wallet,
        "Profile": f"https://polymarket.com/{wallet}",
        "PnL ($)": round(pnl, 2),
        "Volume ($)": round(vol, 2),
        "PnL / Volume (%)": round((pnl / vol) * 100, 3),
    }


# --- HÀM LẤY DỮ LIỆU TỪ API POLYMARKET ---
def fetch_from_leaderboard(lb_category: str, time_period: str, max_pages: int, progress_bar=None):
    """Lấy danh sách trader + PnL + Volume từ /v2/leaderboard bằng cursor pagination."""
    profiles = {}
    errors = []
    page_size = 50
    sort_modes = ["PNL", "VOL"]
    total_steps = max_pages * len(sort_modes)
    step = 0

    for order_by in sort_modes:
        cursor = None
        for page in range(max_pages):
            step += 1
            req_params = {
                "category": lb_category,
                "timePeriod": time_period,
                "orderBy": order_by,
                "limit": page_size,
            }
            if cursor:
                req_params["cursor"] = cursor

            try:
                resp = request_data_api("leaderboard", params=req_params, timeout=12)
                if resp is not None and resp.status_code == 200:
                    payload = resp.json()
                    if isinstance(payload, dict):
                        rows = (
                            payload.get("data")
                            or payload.get("results")
                            or payload.get("leaderboard")
                            or []
                        )
                        next_cursor = (
                            payload.get("next_cursor")
                            or payload.get("nextCursor")
                            or (payload.get("pagination") or {}).get("next_cursor")
                        )
                    else:
                        rows = payload if isinstance(payload, list) else []
                        next_cursor = None

                    if not rows:
                        break

                    for r in rows:
                        parsed = parse_leaderboard_row(r)
                        if parsed and parsed["Wallet"] not in profiles:
                            profiles[parsed["Wallet"]] = parsed

                    if not next_cursor:
                        break
                    cursor = next_cursor
                else:
                    code = resp.status_code if resp is not None else "No Response"
                    body = resp.text[:180] if resp is not None else ""
                    errors.append(f"Leaderboard HTTP {code}: {body}")
                    break
            except Exception as e:
                errors.append(f"Lỗi kết nối Leaderboard: {e}")
                break

            if progress_bar:
                progress_bar.progress(
                    min(step / total_steps, 1.0),
                    text=f"Đang tải Leaderboard v2 ({lb_category} - {order_by}, trang {page + 1}/{max_pages}) — Đã gom {len(profiles)} ví..."
                )
            time.sleep(0.08)

    return profiles, errors


def fetch_wallets_from_holders(tag_slug: str | None, sub_keyword: str, market_limit: int, progress_bar=None):
    """Lấy danh sách ví đang nắm giữ vị thế (/holders, limit=20) tại các kèo đang mở."""
    errors = []
    params = {
        "active": "true",
        "closed": "false",
        "limit": 100,
    }
    if tag_slug:
        params["tag_slug"] = tag_slug

    events = []
    try:
        resp = requests.get(f"{GAMMA_API}/events", params=params, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            events = resp.json()
        else:
            errors.append(f"Gamma API HTTP {resp.status_code}: {resp.text[:120]}")
    except Exception as e:
        errors.append(f"Lỗi kết nối Gamma API: {e}")

    if not events and tag_slug:
        try:
            fb_resp = requests.get(
                f"{GAMMA_API}/events",
                params={"active": "true", "closed": "false", "limit": 200},
                headers=HEADERS,
                timeout=15
            )
            if fb_resp.status_code == 200:
                events = [
                    ev for ev in fb_resp.json()
                    if any(tag_slug in (t.get("slug", "").lower(), t.get("label", "").lower()) for t in ev.get("tags", []))
                ]
        except Exception:
            pass

    kw = sub_keyword.strip().lower()
    markets = []
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
        return [], errors

    wallets = set()
    for idx, m in enumerate(target_markets):
        cid = m.get("conditionId")
        try:
            h_resp = request_data_api(
                "holders",
                params={"market": cid, "limit": 20},
                timeout=10
            )
            if h_resp is not None and h_resp.status_code == 200:
                payload = h_resp.json()
                groups = payload.get("data", payload) if isinstance(payload, dict) else payload
                for token_group in groups:
                    for holder in token_group.get("holders", []):
                        w = (
                            holder.get("proxyWallet")
                            or holder.get("wallet")
                            or holder.get("address")
                            or ""
                        ).strip().lower()
                        if w:
                            wallets.add(w)
            elif not errors and h_resp is not None:
                errors.append(f"Holders API HTTP {h_resp.status_code}: {h_resp.text[:120]}")
        except Exception as e:
            if not errors:
                errors.append(f"Lỗi Holders API: {e}")

        if progress_bar:
            progress_bar.progress(
                (idx + 1) / len(target_markets),
                text=f"Đang quét holders kèo {idx + 1}/{len(target_markets)} — Đã gom {len(wallets)} ví..."
            )
        time.sleep(0.06)

    return list(wallets), errors


def fetch_markets_traded_only(wallet: str) -> int:
    """Lấy số lượng Markets Traded cho một ví."""
    try:
        tr_resp = request_data_api("traded", params={"user": wallet}, timeout=10)
        if tr_resp is not None and tr_resp.status_code == 200:
            data = tr_resp.json()
            if isinstance(data, dict):
                val = data.get("traded") or data.get("marketsTraded") or data.get("count") or 0
                return int(val)
            if isinstance(data, (int, float)):
                return int(data)
    except Exception:
        pass
    return 0


def fetch_single_profile(wallet: str, time_period: str = "ALL"):
    """Lấy đầy đủ PnL, Volume và Markets Traded cho 1 địa chỉ ví."""
    wallet = wallet.strip().lower()
    try:
        lb_resp = request_data_api(
            "leaderboard",
            params={"user": wallet, "timePeriod": time_period},
            timeout=10
        )
        if lb_resp is None or lb_resp.status_code != 200:
            return None

        payload = lb_resp.json()
        rows = (
            payload.get("data") or payload.get("results") or payload.get("leaderboard") or []
            if isinstance(payload, dict)
            else payload
        )
        if not rows:
            return None

        parsed = parse_leaderboard_row(rows[0])
        if not parsed:
            return None

        parsed["Markets Traded"] = fetch_markets_traded_only(wallet)
        return parsed
    except Exception:
        return None


def enrich_leaderboard_profiles(profiles_dict: dict, progress_bar):
    """Bổ sung cột Markets Traded song song cho danh sách ví lấy từ Leaderboard."""
    wallets = list(profiles_dict.keys())
    total = len(wallets)
    if total == 0:
        return pd.DataFrame()

    completed = 0
    with ThreadPoolExecutor(max_workers=8) as executor:
        future_to_wallet = {
            executor.submit(fetch_markets_traded_only, w): w for w in wallets
        }
        for future in as_completed(future_to_wallet):
            w = future_to_wallet[future]
            completed += 1
            profiles_dict[w]["Markets Traded"] = future.result()
            if completed % 10 == 0 or completed == total:
                progress_bar.progress(
                    completed / total,
                    text=f"Đang kiểm tra số thị trường đã đánh: {completed}/{total} ví..."
                )

    return pd.DataFrame(list(profiles_dict.values()))


def scan_wallet_list_concurrent(wallets: list, time_period: str, progress_bar):
    """Phân tích đầy đủ profile cho danh sách địa chỉ ví thu được từ Holders."""
    results = []
    total = len(wallets)
    if total == 0:
        return pd.DataFrame()

    completed = 0
    with ThreadPoolExecutor(max_workers=6) as executor:
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
            value=10000,
            step=100
        )

    st.divider()
    st.subheader("3. Phạm vi quét dữ liệu")
    scan_source = st.radio(
        "Nguồn thu thập ví",
        options=["Bảng xếp hạng (Leaderboard)", "Kèo đang mở (Holders)"],
        index=0,
        help="Leaderboard lấy trực tiếp trader theo Tag; Kèo đang mở quét các ví đang giữ lệnh ở kèo active."
    )
    selected_tag_label = st.selectbox(
        "Category Tag (Polymarket)",
        options=list(POLYMARKET_TAGS.keys()),
        index=0
    )
    sub_keyword = st.text_input(
        "Từ khóa lọc sâu (dùng cho Kèo đang mở)",
        value="",
        placeholder="VD: temperature, bitcoin..."
    )
    time_period = st.selectbox(
        "Khung thời gian tính PnL/Vol",
        options=["ALL", "MONTH", "WEEK"],
        index=0
    )
    scan_depth = st.slider(
        "Độ sâu quét (Số trang Leaderboard / Số kèo)",
        min_value=2,
        max_value=40,
        value=6,
        step=2
    )


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
        tag_info = POLYMARKET_TAGS[selected_tag_label]
        progress = st.progress(0.0, text=f"Đang khởi tạo quét tag {selected_tag_label}...")
        api_errors = []

        if scan_source == "Bảng xếp hạng (Leaderboard)":
            profiles_dict, api_errors = fetch_from_leaderboard(
                lb_category=tag_info["lb_cat"],
                time_period=time_period,
                max_pages=scan_depth,
                progress_bar=progress
            )
            if profiles_dict:
                raw_df = enrich_leaderboard_profiles(profiles_dict, progress_bar=progress)
            else:
                raw_df = pd.DataFrame()
        else:
            wallets, api_errors = fetch_wallets_from_holders(
                tag_slug=tag_info["slug"],
                sub_keyword=sub_keyword,
                market_limit=scan_depth * 3,
                progress_bar=progress
            )
            if wallets:
                raw_df = scan_wallet_list_concurrent(wallets, time_period=time_period, progress_bar=progress)
            else:
                raw_df = pd.DataFrame()

        progress.empty()

        if api_errors:
            for err in set(api_errors):
                st.error(err)

        if raw_df.empty:
            st.warning("Không thu thập được dữ liệu ví nào trong đợt quét này. Bạn hãy thử đổi sang nguồn quét còn lại hoặc chọn Tag khác.")
        else:
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
            st.warning("Đã tải xong dữ liệu ví nhưng chưa có ví nào khớp ngưỡng lọc hiện tại. Hãy nới rộng khoảng PnL/Vol hoặc Markets Traded ở thanh bên trái.")
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
