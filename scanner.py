import hashlib
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import streamlit as st

# ======================== CONFIG & CONSTANTS ========================
st.set_page_config(
    page_title="Polymarket CS2 Signal Scanner",
    page_icon="⚡",
    layout="wide",
)

DEFAULT_WALLET = "0xc9bfc7986767173b4f1c3fc9b2c6b255fc7381c0"
DATA_API_BASE = "https://data-api.polymarket.com"
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"
STATE_FILE = "scanner_state.json"
VN_TZ = timezone(timedelta(hours=7))


# ======================== PERSISTENCE HELPERS ========================
def short_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def load_persisted_ids(active_keys: set[str] | None = None) -> set[str]:
    saved: set[str] = set()
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    saved.update(str(x) for x in data)
        except Exception:
            pass

    qp_raw = st.query_params.get("entered", "")
    if isinstance(qp_raw, list):
        qp_raw = ",".join(qp_raw)
    if qp_raw and active_keys:
        hashes = {h.strip() for h in str(qp_raw).split(",") if h.strip()}
        for k in active_keys:
            if short_hash(k) in hashes:
                saved.add(k)
    return saved


def persist_entered_ids(entered_ids: set[str], active_keys: set[str] | None = None) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(entered_ids), f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    try:
        if entered_ids:
            target_keys = (entered_ids & active_keys) if active_keys else entered_ids
            hashes = sorted({short_hash(k) for k in target_keys})
            if hashes:
                st.query_params["entered"] = ",".join(hashes)
            elif "entered" in st.query_params:
                del st.query_params["entered"]
        else:
            if "entered" in st.query_params:
                del st.query_params["entered"]
    except Exception:
        pass


# ======================== API FETCHERS ========================
def fetch_positions_by_status(wallet: str, status: str) -> list[dict]:
    results: list[dict] = []
    cursor = None
    seen_cursors: set[str] = set()

    for _ in range(25):
        params = {
            "user": wallet.lower().strip(),
            "status": status,
            "limit": 100,
        }
        if cursor:
            params["cursor"] = cursor

        resp = requests.get(f"{DATA_API_BASE}/v2/positions", params=params, timeout=15)
        resp.raise_for_status()
        payload = resp.json()

        if isinstance(payload, list):
            results.extend(payload)
            break

        batch = payload.get("data") or payload.get("positions") or []
        results.extend(batch)

        next_cursor = payload.get("next_cursor") or payload.get("cursor")
        if not next_cursor or next_cursor in seen_cursors or not batch:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    return results


def fetch_gamma_events_meta(
    condition_ids: list[str],
    token_ids: list[str],
    cache: dict[str, dict],
) -> dict[str, dict]:
    missing_cids = [c for c in dict.fromkeys(condition_ids) if c and c not in cache]
    for i in range(0, len(missing_cids), 40):
        chunk = missing_cids[i : i + 40]
        try:
            resp = requests.get(
                f"{GAMMA_API_BASE}/markets",
                params={"condition_ids": ",".join(chunk), "limit": 100},
                timeout=12,
            )
            if resp.ok:
                markets = resp.json()
                if isinstance(markets, list):
                    for m in markets:
                        cid = str(m.get("conditionId") or m.get("condition_id") or "").strip()
                        events = m.get("events") or []
                        ev_title = ""
                        ev_slug = ""
                        if isinstance(events, list) and events:
                            ev_title = str(events[0].get("title") or "").strip()
                            ev_slug = str(events[0].get("slug") or "").strip()
                        if not ev_title:
                            ev_title = str(m.get("question") or "").strip()
                        if not ev_slug:
                            ev_slug = str(m.get("slug") or "").strip()

                        if cid and (ev_title or ev_slug):
                            cache[cid] = {
                                "event_title": ev_title,
                                "event_slug": ev_slug,
                                "question": str(m.get("question") or "").strip(),
                            }
        except Exception:
            pass

    # Fallback lookup via clob_token_ids for any still-missing condition_ids
    still_missing_tokens = []
    token_to_cid: dict[str, str] = {}
    for cid, tid in zip(condition_ids, token_ids):
        if cid and cid not in cache and tid:
            still_missing_tokens.append(tid)
            token_to_cid[tid] = cid

    for i in range(0, len(still_missing_tokens), 40):
        chunk = still_missing_tokens[i : i + 40]
        try:
            resp = requests.get(
                f"{GAMMA_API_BASE}/markets",
                params={"clob_token_ids": ",".join(chunk), "limit": 100},
                timeout=12,
            )
            if resp.ok:
                markets = resp.json()
                if isinstance(markets, list):
                    for m in markets:
                        cid = str(m.get("conditionId") or m.get("condition_id") or "").strip()
                        raw_tids = m.get("clobTokenIds") or []
                        if isinstance(raw_tids, str):
                            try:
                                raw_tids = json.loads(raw_tids)
                            except Exception:
                                raw_tids = []
                        if not cid:
                            for t in raw_tids:
                                if str(t) in token_to_cid:
                                    cid = token_to_cid[str(t)]
                                    break
                        events = m.get("events") or []
                        ev_title = str(events[0].get("title") or "").strip() if events else ""
                        ev_slug = str(events[0].get("slug") or "").strip() if events else ""
                        if not ev_title:
                            ev_title = str(m.get("question") or "").strip()
                        if not ev_slug:
                            ev_slug = str(m.get("slug") or "").strip()
                        if cid and (ev_title or ev_slug):
                            cache[cid] = {
                                "event_title": ev_title,
                                "event_slug": ev_slug,
                                "question": str(m.get("question") or "").strip(),
                            }
        except Exception:
            pass

    return cache


def fetch_clob_books(token_ids: list[str]) -> dict[str, dict]:
    books_map: dict[str, dict] = {}
    unique_tids = [t for t in dict.fromkeys(token_ids) if t]
    for i in range(0, len(unique_tids), 100):
        chunk = unique_tids[i : i + 100]
        payload = [{"token_id": tid} for tid in chunk]
        try:
            resp = requests.post(f"{CLOB_API_BASE}/books", json=payload, timeout=15)
            if resp.ok:
                data = resp.json()
                if isinstance(data, list):
                    for book in data:
                        aid = str(book.get("asset_id") or book.get("token_id") or "").strip()
                        if aid:
                            books_map[aid] = book
        except Exception:
            pass
    return books_map


# ======================== PARSING & FILTER LOGIC ========================
def safe_float(val, default: float = 0.0) -> float:
    try:
        if val is None or val == "":
            return default
        f = float(val)
        return default if math.isnan(f) or math.isinf(f) else f
    except Exception:
        return default


def format_time_ago(raw_ts) -> str:
    if not raw_ts:
        return "—"
    try:
        if isinstance(raw_ts, (int, float)):
            ts = float(raw_ts)
            if ts > 1e12:
                ts /= 1000.0
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        else:
            s = str(raw_ts).strip()
            if s.isdigit():
                ts = float(s)
                if ts > 1e12:
                    ts /= 1000.0
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            else:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
        diff = datetime.now(timezone.utc) - dt
        mins = int(diff.total_seconds() // 60)
        if mins < 0:
            return "Vừa xong"
        if mins < 60:
            return f"{mins}p trước"
        hours = mins // 60
        rem_mins = mins % 60
        if hours < 24:
            return f"{hours}h{rem_mins:02d}p trước"
        days = hours // 24
        return f"{days}d {hours % 24}h trước"
    except Exception:
        return "—"


def extract_team_pair(title: str) -> frozenset[str] | None:
    clean = re.sub(
        r"^(Map\s*\d+\s*Winner|Map\s*Handicap|Total\s*Maps|Match\s*Winner|Winner)\s*:\s*",
        "",
        title,
        flags=re.IGNORECASE,
    )
    clean = re.sub(r"\([^)]*\)", "", clean)
    clean = re.split(r"\s+-\s+|\s+\|\s+", clean)[0].strip()
    m = re.split(r"\s+vs\.?\s+", clean, maxsplit=1, flags=re.IGNORECASE)
    if len(m) == 2:
        t1 = m[0].strip().lower()
        t2 = m[1].strip().lower()
        if len(t1) >= 2 and len(t2) >= 2:
            return frozenset({t1, t2})
    return None


def matches_blocked_league(
    combined_text: str,
    blocked_keywords: list[str],
) -> tuple[bool, str]:
    txt = combined_text.lower()
    # Exclude CCT South America explicitly if default CCT Europe rule is active
    is_cct_sa = "cct south america" in txt or "cct sa" in txt

    for kw in blocked_keywords:
        if kw == "cct europe":
            if not is_cct_sa and (
                re.search(r"\bcct\b.{0,28}\beurope\b", txt)
                or re.search(r"\bcct[\s\-_]*eu\b", txt)
                or "cct europe" in txt
            ):
                return True, "CCT Europe"
        elif kw and kw in txt:
            return True, kw
    return False, ""


def evaluate_positions(
    open_positions: list[dict],
    redeemable_positions: list[dict],
    multiplier: float,
    min_user_bet: float,
    max_user_cap: float,
    max_slippage_cents: float,
    drop_warn_cents: float,
    block_50_60: bool,
    detect_hedge: bool,
    max_fav_prob: int,
    blocked_keywords: list[str],
) -> dict[str, pd.DataFrame]:
    if "gamma_meta_cache" not in st.session_state:
        st.session_state["gamma_meta_cache"] = {}

    cids = [str(p.get("condition_id") or p.get("conditionId") or "").strip() for p in open_positions]
    tids = [str(p.get("token_id") or p.get("asset") or "").strip() for p in open_positions]
    gamma_cache = fetch_gamma_events_meta(cids, tids, st.session_state["gamma_meta_cache"])

    parsed_rows: list[dict] = []
    blocked_team_pairs: set[frozenset[str]] = set()

    for p in open_positions:
        cid = str(p.get("condition_id") or p.get("conditionId") or "").strip()
        tid = str(p.get("token_id") or p.get("asset") or "").strip()
        title = str(p.get("title") or p.get("question") or "Unknown Market").strip()
        outcome = str(p.get("outcome") or p.get("side") or "").strip()
        slug = str(p.get("slug") or p.get("event_slug") or "").strip()

        meta = gamma_cache.get(cid, {})
        ev_title = meta.get("event_title", "")
        ev_slug = meta.get("event_slug", "")
        if not slug and ev_slug:
            slug = ev_slug
        gamma_unverified = not bool(ev_title or ev_slug)

        avg_p = safe_float(p.get("avg_price") or p.get("avgPrice"), 0.0)
        cur_p = safe_float(p.get("current_price") or p.get("curPrice"), 0.0)
        cur_size = safe_float(p.get("current_size") or p.get("size"), 0.0)
        cur_val = safe_float(p.get("current_value") or p.get("currentValue"), 0.0)
        if cur_p <= 0 and cur_size > 0 and cur_val > 0:
            cur_p = cur_val / cur_size

        entry_cost = safe_float(
            p.get("entry_cost_usdc") or p.get("initial_value") or p.get("initialValue"),
            0.0,
        )
        if entry_cost <= 0 and cur_size > 0 and avg_p > 0:
            entry_cost = cur_size * avg_p

        realized_pnl = safe_float(p.get("realized_pnl") or p.get("realizedPnl"), 0.0)
        unrealized_pnl = safe_float(p.get("unrealized_pnl") or p.get("unrealizedPnl"), 0.0)
        if unrealized_pnl == 0.0 and cur_val > 0 and entry_cost > 0:
            unrealized_pnl = cur_val - entry_cost
        total_pnl = realized_pnl + unrealized_pnl

        last_ts = (
            p.get("last_event_at")
            or p.get("updated_at")
            or p.get("created_at")
            or p.get("timestamp")
        )
        pos_key = f"{cid}_{tid}_{outcome}"
        url = f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com"

        combined_text = f"{title} | {ev_title} | {slug} | {ev_slug}"
        is_blk, blk_reason = matches_blocked_league(combined_text, blocked_keywords)
        team_pair = extract_team_pair(title)
        if is_blk and team_pair:
            blocked_team_pairs.add(team_pair)

        parsed_rows.append(
            {
                "pos_key": pos_key,
                "condition_id": cid,
                "token_id": tid,
                "title": title,
                "event_title": ev_title,
                "outcome": outcome,
                "slug": slug,
                "url": url,
                "avg_price": avg_p,
                "cur_price": cur_p,
                "entry_cost_usdc": entry_cost,
                "total_pnl": total_pnl,
                "last_buy_ago": format_time_ago(last_ts),
                "gamma_unverified": gamma_unverified,
                "is_blocked_direct": is_blk,
                "blocked_reason": blk_reason,
                "team_pair": team_pair,
            }
        )

    # Layer 3: Cross-lock secondary markets matching blocked league team pairs
    for row in parsed_rows:
        if not row["is_blocked_direct"] and row["team_pair"] and row["team_pair"] in blocked_team_pairs:
            row["is_blocked_direct"] = True
            row["blocked_reason"] = "Khóa chéo cặp đội CCT EU"

    # Count active outcomes per condition_id for Hedge detection
    cid_outcomes: dict[str, set[str]] = {}
    for row in parsed_rows:
        if 0.02 < row["cur_price"] < 0.98 and not row["is_blocked_direct"]:
            cid_outcomes.setdefault(row["condition_id"], set()).add(row["outcome"])

    # Pre-filter candidates needing CLOB orderbook lookup
    candidate_tids: list[str] = []
    for row in parsed_rows:
        if not (0.02 < row["cur_price"] < 0.98):
            continue
        if row["is_blocked_direct"]:
            continue
        if detect_hedge and len(cid_outcomes.get(row["condition_id"], set())) >= 2:
            continue
        raw_bet = row["entry_cost_usdc"] * multiplier
        if raw_bet < min_user_bet:
            continue
        avg_p = row["avg_price"]
        if block_50_60 and (0.50 <= avg_p <= 0.60):
            continue
        if avg_p > (max_fav_prob / 100.0) or avg_p <= 0.0:
            continue
        candidate_tids.append(row["token_id"])

    books_map = fetch_clob_books(candidate_tids)

    valid_list: list[dict] = []
    slip_list: list[dict] = []
    hedge_list: list[dict] = []
    blocked_list: list[dict] = []
    rejected_list: list[dict] = []

    for row in parsed_rows:
        cur_p = row["cur_price"]
        avg_p = row["avg_price"]
        entry_cost = row["entry_cost_usdc"]
        raw_bet = entry_cost * multiplier
        final_bet = round(min(raw_bet, max_user_cap), 2)

        # 1. Resolved positions waiting on-chain Redeem
        if cur_p >= 0.98 or cur_p <= 0.02:
            row["reject_reason"] = f"Đã ngã ngũ (giá {cur_p * 100:.1f}¢)"
            rejected_list.append(row)
            continue

        # 2. Blocked League (3-layer CCT Europe defense)
        if row["is_blocked_direct"]:
            cond_notes = []
            if raw_bet < min_user_bet:
                cond_notes.append(f"< Min ${min_user_bet:.2f}")
            if block_50_60 and (0.50 <= avg_p <= 0.60):
                cond_notes.append("Dải 50-60%")
            elif avg_p > (max_fav_prob / 100.0):
                cond_notes.append(f"> {max_fav_prob}%")
            row["raw_user_bet"] = round(raw_bet, 2)
            row["other_cond"] = (
                f"Đạt vốn & giá ({row['blocked_reason']})"
                if not cond_notes
                else f"Thêm lỗi: {', '.join(cond_notes)}"
            )
            blocked_list.append(row)
            continue

        # 3. Hedge 2 sides on same condition_id
        if detect_hedge and len(cid_outcomes.get(row["condition_id"], set())) >= 2:
            row["raw_user_bet"] = round(raw_bet, 2)
            row["hedge_note"] = "Ví gốc đang mở >= 2 cửa cùng trận"
            hedge_list.append(row)
            continue

        # 4. Minimum Bet / Whale Threshold
        if raw_bet < min_user_bet:
            row["reject_reason"] = (
                f"Vốn ví ${entry_cost:,.0f} -> ${raw_bet:.2f} < Min ${min_user_bet:.2f}"
            )
            rejected_list.append(row)
            continue

        # 5. Price Band Filter
        if block_50_60 and (0.50 <= avg_p <= 0.60):
            row["reject_reason"] = f"Vùng cấm 50%–60% ({avg_p * 100:.1f}¢)"
            rejected_list.append(row)
            continue
        if avg_p > (max_fav_prob / 100.0) or avg_p <= 0.0:
            row["reject_reason"] = f"Ngoài dải giá cho phép ({avg_p * 100:.1f}¢)"
            rejected_list.append(row)
            continue

        # 6. CLOB Orderbook Evaluation
        max_limit_price = min(avg_p + (max_slippage_cents / 100.0), 0.99)
        max_limit_cents = round(max_limit_price * 100.0, 1)

        book = books_map.get(row["token_id"], {})
        asks_raw = book.get("asks") or []
        parsed_asks: list[tuple[float, float]] = []
        for a in asks_raw:
            ap = safe_float(a.get("price"), 0.0)
            asz = safe_float(a.get("size"), 0.0)
            if ap > 0 and asz > 0:
                parsed_asks.append((ap, asz))
        parsed_asks.sort(key=lambda x: x[0])

        notes: list[str] = []
        if row["gamma_unverified"]:
            notes.append("⚠️ Chưa xác minh giải (Gamma)")

        if parsed_asks:
            best_ask = parsed_asks[0][0]
            avail_liq_usdc = sum(
                ap * asz for ap, asz in parsed_asks if ap <= max_limit_price + 1e-6
            )
            exec_price = best_ask
            is_empty_ask = False
        else:
            avail_liq_usdc = 0.0
            exec_price = cur_p if (0.02 < cur_p < 0.98) else avg_p
            is_empty_ask = True
            notes.append("📋 Trống Ask (Kê Limit)")

        slippage_cents = round((exec_price - avg_p) * 100.0, 1)
        drop_cents = round((avg_p - exec_price) * 100.0, 1)

        if drop_cents > drop_warn_cents:
            notes.append(f"⚠️ Tụt >{drop_warn_cents:.0f}¢ — CHECK LIVE!")

        is_slippage_over = slippage_cents > max_slippage_cents
        if not is_empty_ask and not is_slippage_over and avail_liq_usdc < final_bet:
            notes.append(f"💧 Ask mỏng (${avail_liq_usdc:.2f})")

        # Min 5 shares CLOB Limit warning
        ref_limit_price = max(max_limit_price, 0.01)
        est_shares = final_bet / ref_limit_price
        needs_limit = is_empty_ask or is_slippage_over or (avail_liq_usdc < final_bet)
        if needs_limit and est_shares < 5.0:
            min_limit_usdc = 5.0 * ref_limit_price
            notes.append(f"⚠️ Limit cần >=5sh (~${min_limit_usdc:.2f})")

        if not notes:
            notes.append("✅ Khớp đẹp")

        row["final_user_bet"] = final_bet
        row["exec_price"] = exec_price
        row["slippage_cents"] = slippage_cents
        row["max_limit_cents"] = max_limit_cents
        row["avail_liq_usdc"] = round(avail_liq_usdc, 2)
        row["action_note"] = " | ".join(notes)

        if is_slippage_over:
            slip_list.append(row)
        else:
            valid_list.append(row)

    return {
        "valid": pd.DataFrame(valid_list),
        "slip": pd.DataFrame(slip_list),
        "hedge": pd.DataFrame(hedge_list),
        "blocked": pd.DataFrame(blocked_list),
        "rejected": pd.DataFrame(rejected_list),
        "redeemable_count": len(redeemable_positions),
    }


# ======================== SESSION STATE INIT ========================
if "entered_ids" not in st.session_state:
    st.session_state["entered_ids"] = load_persisted_ids()
if "editor_ver" not in st.session_state:
    st.session_state["editor_ver"] = 0
if "scan_results" not in st.session_state:
    st.session_state["scan_results"] = None
if "last_scan_time" not in st.session_state:
    st.session_state["last_scan_time"] = None


# ======================== SIDEBAR CONFIG ========================
with st.sidebar:
    st.header("⚙️ Cấu Hình Bộ 5 Quy Tắc")
    wallet_input = st.text_input("Địa chỉ ví mục tiêu", value=DEFAULT_WALLET).strip()

    multiplier = st.number_input(
        "Hệ số nhân quy mô (QT 5)",
        min_value=0.00001,
        max_value=1.0,
        value=0.00100,
        step=0.00010,
        format="%.5f",
        help="Mặc định 0.00100 cho vốn $1,000: lệnh >= $2,000 ví gốc tự khớp tối thiểu $2.00.",
    )

    col_s1, col_s2 = st.columns(2)
    min_user_bet = col_s1.number_input(
        "Lệnh tối thiểu ($) (QT 1)",
        min_value=0.1,
        value=2.00,
        step=0.5,
        help="Với hệ số 0.00100, $2.00 tương đương vốn ví gốc >= $2,000 (chặn vùng lỗ $1k-$2k).",
    )
    max_user_cap = col_s2.number_input(
        "Trần tối đa ($) (QT 3)",
        min_value=1.0,
        value=20.00,
        step=1.0,
        help="Chốt trần $20.00/kèo (2% vốn $1,000; tương đương $20,000 ví gốc).",
    )

    max_slippage_cents = st.slider(
        "Trượt giá Ask tối đa (cent) (QT 4)",
        min_value=0.5,
        max_value=10.0,
        value=2.5,
        step=0.5,
        help="So sánh Best Ask thực tế trên CLOB với giá trung bình ví gốc.",
    )

    drop_warn_cents = st.slider(
        "Cảnh báo giá tụt sâu trên (cent) (QT 4)",
        min_value=2.0,
        max_value=15.0,
        value=5.0,
        step=1.0,
        help="Tự gắn cờ cảnh báo kiểm tra trận đã đánh Live chưa nếu giá tụt sâu.",
    )

    block_50_60 = st.checkbox("Chặn dải lỗ 50% – 60% (QT 2)", value=True)
    detect_hedge = st.checkbox("Chặn kèo ví gốc Hedge 2 đầu", value=True)
    max_fav_prob = st.slider(
        "Chặn kèo chắc ăn trên (%) (QT 2)",
        min_value=70,
        max_value=99,
        value=85,
        step=5,
    )

    blocked_kw_input = st.text_input(
        "Từ khóa giải chặn — Bảng riêng (QT 2)",
        value="CCT Europe",
        help="Phòng thủ 3 lớp: chặn cả biến thể CCT EU, kèo phụ qua Gamma API và khóa chéo theo cặp đội.",
    )
    blocked_keywords = [k.strip().lower() for k in blocked_kw_input.split(",") if k.strip()]

    st.divider()
    min_whale_thr = min_user_bet / multiplier if multiplier > 0 else 0
    max_whale_thr = max_user_cap / multiplier if multiplier > 0 else 0
    st.caption(
        f"**Ngưỡng ví gốc:** Từ **${min_whale_thr:,.0f}** "
        f"đến **${max_whale_thr:,.0f}** (đạt trần ${max_user_cap:.2f})."
    )

    if st.session_state["entered_ids"]:
        if st.button(
            f"🧹 Xóa dấu Đã vào ({len(st.session_state['entered_ids'])})",
            use_container_width=True,
        ):
            st.session_state["entered_ids"] = set()
            st.session_state["editor_ver"] += 1
            persist_entered_ids(set(), active_keys=set())
            st.rerun()


# ======================== MAIN UI ========================
st.title("⚡ Trạm Quét Lệnh Copy-Trade CS2")

col_btn, col_time = st.columns([2, 3])
scan_clicked = col_btn.button("🚀 QUÉT LỆNH HỢP LỆ NGAY", type="primary", use_container_width=True)

if scan_clicked or st.session_state["scan_results"] is None:
    with st.spinner("Đang kéo dữ liệu Polymarket Data API v2, Gamma & Sổ lệnh CLOB..."):
        try:
            open_pos = fetch_positions_by_status(wallet_input, "OPEN")
            redeem_pos = fetch_positions_by_status(wallet_input, "REDEEMABLE")
            res = evaluate_positions(
                open_positions=open_pos,
                redeemable_positions=redeem_pos,
                multiplier=multiplier,
                min_user_bet=min_user_bet,
                max_user_cap=max_user_cap,
                max_slippage_cents=max_slippage_cents,
                drop_warn_cents=drop_warn_cents,
                block_50_60=block_50_60,
                detect_hedge=detect_hedge,
                max_fav_prob=max_fav_prob,
                blocked_keywords=blocked_keywords,
            )
            st.session_state["scan_results"] = res
            st.session_state["last_scan_time"] = datetime.now(VN_TZ).strftime("%H:%M:%S %d/%m/%Y (GMT+7)")

            # Merge persisted state with active keys
            active_keys: set[str] = set()
            for k_df in ("valid", "slip"):
                if not res[k_df].empty:
                    active_keys.update(res[k_df]["pos_key"].tolist())
            st.session_state["entered_ids"] = load_persisted_ids(active_keys)
        except Exception as exc:
            st.error(f"Lỗi kết nối Polymarket API: {exc}")

if st.session_state["last_scan_time"]:
    col_time.info(f"🕒 Cập nhật lần cuối lúc: **{st.session_state['last_scan_time']}**")

min_whale_disp = min_user_bet / multiplier if multiplier > 0 else 0
with st.expander("📋 Bảng Tóm Tắt 5 Quy Tắc & Cảnh Báo Thủ Công (Bấm để xem/thu gọn)", expanded=False):
    rule_col1, rule_col2 = st.columns(2)
    with rule_col1:
        st.markdown(
            "**🤖 Tool đã tự động lọc 100%:**\n"
            f"* **QT 1 (Ngưỡng vào):** Chỉ lấy kèo ví gốc >= `${min_whale_disp:,.0f}` "
            f"(bạn vào >= `${min_user_bet:.2f}`, hệ số `{multiplier:.5f}`).\n"
            f"* **QT 2 (Dải giá & Giải):** Chỉ lấy `< 50%` hoặc `60% – {max_fav_prob}%`; "
            f"chặn `50%–60%`, `> {max_fav_prob}%`, kèo Hedge 2 đầu và phòng thủ 3 lớp với **{blocked_kw_input}**.\n"
            f"* **QT 3 (Trần rủi ro):** Khóa tối đa **`${max_user_cap:.2f}/kèo`** "
            f"(`min(Vốn ví × {multiplier:.5f}, ${max_user_cap:.2f})`).\n"
            f"* **QT 4 (Trượt giá CLOB):** Đối soát trực tiếp **Best Ask** & **Thanh khoản** trên sổ lệnh; "
            f"chặn Ask cao hơn ví gốc quá **`+{max_slippage_cents:.1f}¢`**."
        )
    with rule_col2:
        st.warning(
            "**⚠️ 3 việc BẮT BUỘC tự kiểm tra (Tool không tự chặn được):**\n"
            f"1. **Không vào kèo Live (QT 4):** Mở link Polymarket kiểm tra trận chưa bắt đầu thi đấu "
            f"(đặc biệt khi thấy cờ `⚠️ Tụt >{drop_warn_cents:.0f}¢`).\n"
            "2. **Giữ tới đáo hạn (QT 4):** Khớp xong giữ nguyên tới hết trận để bấm **Redeem**, không bán non giữa trận.\n"
            f"3. **Kỷ luật hệ số `{multiplier:.5f}` & trần `${max_user_cap:.0f}` (QT 5):** "
            "Giữ ổn định quy mô theo vốn `$1,000`, không nhồi tay ngoài công thức."
        )

res = st.session_state["scan_results"]
if res is not None:
    valid_df: pd.DataFrame = res["valid"].copy()
    slip_df: pd.DataFrame = res["slip"].copy()
    hedge_df: pd.DataFrame = res["hedge"].copy()
    blocked_df: pd.DataFrame = res["blocked"].copy()
    rejected_df: pd.DataFrame = res["rejected"].copy()
    redeem_cnt: int = res["redeemable_count"]

    entered_set: set[str] = st.session_state["entered_ids"]
    active_keys_all: set[str] = set()
    if not valid_df.empty:
        active_keys_all.update(valid_df["pos_key"].tolist())
    if not slip_df.empty:
        active_keys_all.update(slip_df["pos_key"].tolist())

    remaining_capital = 0.0
    if not valid_df.empty:
        remaining_capital = float(
            valid_df.loc[~valid_df["pos_key"].isin(entered_set), "final_user_bet"].sum()
        )

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("🟢 Kèo hợp lệ ngay", len(valid_df))
    m2.metric("💰 Vốn cần thêm ($)", f"${remaining_capital:.2f}")
    m3.metric("🟡 Quá trượt giá (Limit)", len(slip_df))
    m4.metric("🚫 Giải chặn (CCT EU)", len(blocked_df))
    m5.metric("🏆 Chờ Redeem", redeem_cnt)

    st.divider()

    # --- MỤC 1: KÈO HỢP LỆ ĐỂ VÀO LỆNH NGAY ---
    st.subheader(f"🟢 1. Kèo Hợp Lệ Để Vào Lệnh Ngay ({len(valid_df)})")
    if valid_df.empty:
        st.info("Hiện chưa có kèo nào khớp đủ Bộ 5 Quy Tắc và nằm trong ngưỡng trượt giá Ask.")
    else:
        valid_df["entered"] = valid_df["pos_key"].isin(entered_set)
        disp_valid = valid_df[
            [
                "pos_key",
                "entered",
                "title",
                "outcome",
                "final_user_bet",
                "action_note",
                "avg_price",
                "exec_price",
                "slippage_cents",
                "max_limit_cents",
                "avail_liq_usdc",
                "entry_cost_usdc",
                "last_buy_ago",
                "url",
            ]
        ].copy()
        disp_valid["avg_price"] = (disp_valid["avg_price"] * 100.0).round(1)
        disp_valid["exec_price"] = (disp_valid["exec_price"] * 100.0).round(1)

        edited_valid = st.data_editor(
            disp_valid,
            key=f"valid_editor_{st.session_state['editor_ver']}",
            use_container_width=True,
            hide_index=True,
            column_order=[
                "entered",
                "title",
                "outcome",
                "final_user_bet",
                "action_note",
                "avg_price",
                "exec_price",
                "slippage_cents",
                "max_limit_cents",
                "avail_liq_usdc",
                "entry_cost_usdc",
                "last_buy_ago",
                "url",
            ],
            column_config={
                "entered": st.column_config.CheckboxColumn("✅ Đã vào", default=False),
                "title": st.column_config.TextColumn("Trận đấu / Kèo", width="medium"),
                "outcome": st.column_config.TextColumn("Cửa chọn", width="small"),
                "final_user_bet": st.column_config.NumberColumn("Tiền bạn vào ($)", format="$%.2f"),
                "action_note": st.column_config.TextColumn("Trạng thái sổ lệnh", width="medium"),
                "avg_price": st.column_config.NumberColumn("Giá ví gốc (¢)", format="%.1f¢"),
                "exec_price": st.column_config.NumberColumn("Best Ask (¢)", format="%.1f¢"),
                "slippage_cents": st.column_config.NumberColumn("Trượt giá (¢)", format="%+.1f¢"),
                "max_limit_cents": st.column_config.NumberColumn("🛑 Giá trần (¢)", format="%.1f¢"),
                "avail_liq_usdc": st.column_config.NumberColumn("Thanh khoản Ask ($)", format="$%.2f"),
                "entry_cost_usdc": st.column_config.NumberColumn("Vốn ví gốc ($)", format="$%,.0f"),
                "last_buy_ago": st.column_config.TextColumn("Cập nhật", width="small"),
                "url": st.column_config.LinkColumn("Polymarket", display_text="Mở kèo ↗"),
            },
            disabled=[
                "pos_key",
                "title",
                "outcome",
                "final_user_bet",
                "action_note",
                "avg_price",
                "exec_price",
                "slippage_cents",
                "max_limit_cents",
                "avail_liq_usdc",
                "entry_cost_usdc",
                "last_buy_ago",
                "url",
            ],
        )

        new_entered_valid = set(edited_valid.loc[edited_valid["entered"], "pos_key"].tolist())
        old_entered_valid = set(disp_valid.loc[disp_valid["entered"], "pos_key"].tolist())
        if new_entered_valid != old_entered_valid:
            updated_set = (entered_set - old_entered_valid) | new_entered_valid
            st.session_state["entered_ids"] = updated_set
            persist_entered_ids(updated_set, active_keys_all)
            st.rerun()

    # --- MỤC 2: KÈO ĐẠT ĐIỀU KIỆN NHƯNG QUÁ TRƯỢT GIÁ ASK ---
    if not slip_df.empty:
        st.subheader(f"🟡 2. Kèo Đạt Điều Kiện Nhưng Quá Trượt Giá Ask ({len(slip_df)})")
        st.caption(
            "Giá Ask trên sổ lệnh đang cao hơn trần cho phép; chỉ đặt lệnh Limit ở cột **🛑 Giá trần** nếu trận chưa đá Live."
        )
        slip_df["entered"] = slip_df["pos_key"].isin(st.session_state["entered_ids"])
        disp_slip = slip_df[
            [
                "pos_key",
                "entered",
                "title",
                "outcome",
                "final_user_bet",
                "action_note",
                "avg_price",
                "exec_price",
                "slippage_cents",
                "max_limit_cents",
                "entry_cost_usdc",
                "last_buy_ago",
                "url",
            ]
        ].copy()
        disp_slip["avg_price"] = (disp_slip["avg_price"] * 100.0).round(1)
        disp_slip["exec_price"] = (disp_slip["exec_price"] * 100.0).round(1)

        edited_slip = st.data_editor(
            disp_slip,
            key=f"slip_editor_{st.session_state['editor_ver']}",
            use_container_width=True,
            hide_index=True,
            column_order=[
                "entered",
                "title",
                "outcome",
                "final_user_bet",
                "action_note",
                "avg_price",
                "exec_price",
                "slippage_cents",
                "max_limit_cents",
                "entry_cost_usdc",
                "last_buy_ago",
                "url",
            ],
            column_config={
                "entered": st.column_config.CheckboxColumn("✅ Đã kê Limit", default=False),
                "title": st.column_config.TextColumn("Trận đấu / Kèo", width="medium"),
                "outcome": st.column_config.TextColumn("Cửa chọn", width="small"),
                "final_user_bet": st.column_config.NumberColumn("Tiền bạn vào ($)", format="$%.2f"),
                "action_note": st.column_config.TextColumn("Ghi chú", width="medium"),
                "avg_price": st.column_config.NumberColumn("Giá ví gốc (¢)", format="%.1f¢"),
                "exec_price": st.column_config.NumberColumn("Best Ask (¢)", format="%.1f¢"),
                "slippage_cents": st.column_config.NumberColumn("Trượt giá (¢)", format="%+.1f¢"),
                "max_limit_cents": st.column_config.NumberColumn("🛑 Giá trần (¢)", format="%.1f¢"),
                "entry_cost_usdc": st.column_config.NumberColumn("Vốn ví gốc ($)", format="$%,.0f"),
                "last_buy_ago": st.column_config.TextColumn("Cập nhật", width="small"),
                "url": st.column_config.LinkColumn("Polymarket", display_text="Mở kèo ↗"),
            },
            disabled=[
                "pos_key",
                "title",
                "outcome",
                "final_user_bet",
                "action_note",
                "avg_price",
                "exec_price",
                "slippage_cents",
                "max_limit_cents",
                "entry_cost_usdc",
                "last_buy_ago",
                "url",
            ],
        )

        new_entered_slip = set(edited_slip.loc[edited_slip["entered"], "pos_key"].tolist())
        old_entered_slip = set(disp_slip.loc[disp_slip["entered"], "pos_key"].tolist())
        if new_entered_slip != old_entered_slip:
            updated_set = (st.session_state["entered_ids"] - old_entered_slip) | new_entered_slip
            st.session_state["entered_ids"] = updated_set
            persist_entered_ids(updated_set, active_keys_all)
            st.rerun()

    # --- MỤC 3: CẢNH BÁO VÍ GỐC ĐANG HEDGE 2 ĐẦU ---
    if not hedge_df.empty:
        st.subheader(f"🟠 3. Cảnh Báo Ví Gốc Đang Hedge 2 Đầu ({len(hedge_df)})")
        disp_hedge = hedge_df[
            ["title", "outcome", "entry_cost_usdc", "raw_user_bet", "avg_price", "cur_price", "hedge_note", "url"]
        ].copy()
        disp_hedge["avg_price"] = (disp_hedge["avg_price"] * 100.0).round(1)
        disp_hedge["cur_price"] = (disp_hedge["cur_price"] * 100.0).round(1)
        st.dataframe(
            disp_hedge,
            use_container_width=True,
            hide_index=True,
            column_config={
                "title": "Trận đấu",
                "outcome": "Cửa",
                "entry_cost_usdc": st.column_config.NumberColumn("Vốn ví gốc ($)", format="$%,.0f"),
                "raw_user_bet": st.column_config.NumberColumn("Quy đổi ($)", format="$%.2f"),
                "avg_price": st.column_config.NumberColumn("Giá mua (¢)", format="%.1f¢"),
                "cur_price": st.column_config.NumberColumn("Giá hiện tại (¢)", format="%.1f¢"),
                "hedge_note": "Lý do chặn",
                "url": st.column_config.LinkColumn("Polymarket", display_text="Mở kèo ↗"),
            },
        )

    # --- MỤC 4: BẢNG THEO DÕI RIÊNG GIẢI CHẶN (CCT EUROPE) ---
    if not blocked_df.empty:
        st.subheader(f"🚫 4. Bảng Theo Dõi Riêng: Giải Chặn — {blocked_kw_input} ({len(blocked_df)})")
        disp_blk = blocked_df[
            [
                "title",
                "outcome",
                "entry_cost_usdc",
                "raw_user_bet",
                "other_cond",
                "avg_price",
                "cur_price",
                "total_pnl",
                "url",
            ]
        ].copy()
        disp_blk["avg_price"] = (disp_blk["avg_price"] * 100.0).round(1)
        disp_blk["cur_price"] = (disp_blk["cur_price"] * 100.0).round(1)
        st.dataframe(
            disp_blk,
            use_container_width=True,
            hide_index=True,
            column_config={
                "title": "Trận đấu (Giải chặn)",
                "outcome": "Cửa",
                "entry_cost_usdc": st.column_config.NumberColumn("Vốn ví gốc ($)", format="$%,.0f"),
                "raw_user_bet": st.column_config.NumberColumn(f"Quy đổi (×{multiplier:.5f})", format="$%.2f"),
                "other_cond": "Đánh giá điều kiện khác",
                "avg_price": st.column_config.NumberColumn("Giá ví gốc (¢)", format="%.1f¢"),
                "cur_price": st.column_config.NumberColumn("Giá hiện tại (¢)", format="%.1f¢"),
                "total_pnl": st.column_config.NumberColumn("PnL tạm tính ví gốc ($)", format="$%+,.2f"),
                "url": st.column_config.LinkColumn("Polymarket", display_text="Mở kèo ↗"),
            },
        )

    # --- MỤC 5: CÁC KÈO BỊ LOẠI BỞI BỘ LỌC ---
    if not rejected_df.empty:
        with st.expander(f"⚪ 5. Xem Danh Sách Kèo Đã Loại Bỏ ({len(rejected_df)})", expanded=False):
            disp_rej = rejected_df[
                ["title", "outcome", "entry_cost_usdc", "avg_price", "cur_price", "reject_reason"]
            ].copy()
            disp_rej["avg_price"] = (disp_rej["avg_price"] * 100.0).round(1)
            disp_rej["cur_price"] = (disp_rej["cur_price"] * 100.0).round(1)
            st.dataframe(
                disp_rej,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "title": "Trận đấu",
                    "outcome": "Cửa",
                    "entry_cost_usdc": st.column_config.NumberColumn("Vốn ví gốc ($)", format="$%,.0f"),
                    "avg_price": st.column_config.NumberColumn("Giá mua (¢)", format="%.1f¢"),
                    "cur_price": st.column_config.NumberColumn("Giá hiện tại (¢)", format="%.1f¢"),
                    "reject_reason": "Lý do loại",
                },
            )
