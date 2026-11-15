import requests

TARGET_WALLET = "0xc9bfc7986767173b4f1c3fc9b2c6b255fc7381c0"
URL = "https://data-api.polymarket.com/closed-positions"

def fetch_all_closed_positions(wallet: str):
    all_positions = []
    offset = 0
    limit = 50  # Giới hạn mỗi trang của API

    while True:
        params = {
            "user": wallet,
            "limit": limit,
            "offset": offset,
            "sortBy": "TIMESTAMP",
            "sortDirection": "DESC"
        }
        res = requests.get(URL, params=params, timeout=15)
        res.raise_for_status()
        data = res.json()

        if not data:
            break
        all_positions.extend(data)
        if len(data) < limit:
            break
        offset += limit

    return all_positions

def analyze_buckets(positions):
    buckets = {
        "Dưới 50% (Underdog)": {"total": 0, "wins": 0, "cost": 0.0, "pnl": 0.0},
        "Trên 50% (Favorite)": {"total": 0, "wins": 0, "cost": 0.0, "pnl": 0.0},
        "Đúng 50%": {"total": 0, "wins": 0, "cost": 0.0, "pnl": 0.0},
    }

    for p in positions:
        avg_price = float(p.get("avgPrice", 0))
        pnl = float(p.get("realizedPnl", 0))
        shares = float(p.get("totalBought", 0))
        cost = avg_price * shares

        if avg_price < 0.5:
            key = "Dưới 50% (Underdog)"
        elif avg_price > 0.5:
            key = "Trên 50% (Favorite)"
        else:
            key = "Đúng 50%"

        buckets[key]["total"] += 1
        if pnl > 0:
            buckets[key]["wins"] += 1
        buckets[key]["cost"] += cost
        buckets[key]["pnl"] += pnl

    for name, stats in buckets.items():
        total = stats["total"]
        if total == 0:
            continue
        win_rate = (stats["wins"] / total) * 100
        roi = (stats["pnl"] / stats["cost"] * 100) if stats["cost"] > 0 else 0
        print(f"=== {name} ===")
        print(f"  Số vị thế đã đóng : {total} (Thắng: {stats['wins']} | Thua: {total - stats['wins']})")
        print(f"  Tỷ lệ thắng       : {win_rate:.2f}%")
        print(f"  Tổng vốn vào      : ${stats['cost']:,.2f}")
        print(f"  Lời/Lỗ ròng (PnL) : ${stats['pnl']:+,.2f} (ROI: {roi:+.2f}%)\n")

if __name__ == "__main__":
    data = fetch_all_closed_positions(TARGET_WALLET)
    print(f"Tổng số vị thế đã đóng tìm thấy: {len(data)}\n")
    analyze_buckets(data)
