#!/usr/bin/env python3
"""動能/價量篩選腳本 — 從自選股清單中找出符合多頭排列、價量齊揚的候選股。

設計原則
--------
* 零外部相依：只用標準函式庫（urllib / json / csv），跟 fetch_data.py 一致。
* 這不是預測工具：只是依規則（MA 排列、近期漲幅、量比、RSI）排序候選股，
  不代表未來會漲，使用者仍需自行做基本面/籌碼確認。
* 沙箱限制：撰寫環境連不到下列資料端點，未對線上 API 實測過，第一次在本機
  或 Actions 跑時可能需要依實際回應格式微調。

資料源
------
台股：FinMind TaiwanStockPrice（https://api.finmindtrade.com/api/v4/data）
美股：stooq.com 免費日線 CSV（https://stooq.com/q/d/l/?s=<ticker>.us&i=d）

用法
----
    python3 screener.py --market tw --tickers 2330,2317,2454
    python3 screener.py --market us --tickers AAPL,MSFT,NVDA
    python3 screener.py --market tw --watchlist watchlist_tw.txt
"""

import argparse
import csv
import io
import json
import ssl
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

_CTX = ssl.create_default_context()
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}


def _get(url, retries=3, timeout=20):
    last_exc = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=BROWSER_HEADERS)
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise last_exc


# ---- 資料抓取 -------------------------------------------------------------

def fetch_tw_prices(ticker, days=120):
    """FinMind TaiwanStockPrice，回傳由舊到新的 [(date, close, volume), ...]。"""
    start = (datetime.now(timezone.utc) - timedelta(days=days * 2)).strftime("%Y-%m-%d")
    url = (
        "https://api.finmindtrade.com/api/v4/data?"
        + urllib.parse.urlencode({
            "dataset": "TaiwanStockPrice",
            "data_id": ticker,
            "start_date": start,
        })
    )
    obj = json.loads(_get(url))
    rows = obj.get("data", [])
    out = [(r["date"], float(r["close"]), float(r["Trading_Volume"])) for r in rows if r.get("close")]
    return out[-days:]


def fetch_us_prices(ticker, days=120):
    """stooq.com 免費日線 CSV，回傳由舊到新的 [(date, close, volume), ...]。"""
    url = f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d"
    text = _get(url)
    reader = csv.DictReader(io.StringIO(text))
    out = []
    for row in reader:
        try:
            out.append((row["Date"], float(row["Close"]), float(row["Volume"])))
        except (KeyError, ValueError):
            continue
    return out[-days:]


# ---- 指標計算 -------------------------------------------------------------

def sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(-n, 0):
        diff = closes[i] - closes[i - 1]
        (gains if diff > 0 else losses).append(abs(diff))
    avg_gain = sum(gains) / n
    avg_loss = sum(losses) / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 1)


def score_ticker(rows):
    """依價量規則打分，回傳 dict 或 None（資料不足時跳過）。"""
    if len(rows) < 65:
        return None
    closes = [c for _, c, _ in rows]
    vols = [v for _, _, v in rows]

    ma20 = sma(closes, 20)
    ma60 = sma(closes, 60)
    last = closes[-1]
    ret_20d = (last / closes[-21] - 1) * 100 if len(closes) > 20 else None
    vol_ratio = vols[-1] / (sum(vols[-21:-1]) / 20) if sum(vols[-21:-1]) else None
    rsi14 = rsi(closes)

    if None in (ma20, ma60, ret_20d, vol_ratio, rsi14):
        return None

    bullish_alignment = last > ma20 > ma60
    not_overheated = rsi14 < 80

    if not (bullish_alignment and not_overheated):
        return None

    # 簡單加權分數：漲幅 + 量增，RSI 過熱輕微扣分
    score = ret_20d + min(vol_ratio, 3) * 5 - max(0, rsi14 - 70)

    return {
        "close": round(last, 2),
        "ma20": round(ma20, 2),
        "ma60": round(ma60, 2),
        "ret_20d_pct": round(ret_20d, 1),
        "vol_ratio": round(vol_ratio, 2),
        "rsi14": rsi14,
        "score": round(score, 1),
    }


# ---- 主流程 ---------------------------------------------------------------

def load_watchlist(path):
    with open(path, encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def run(market, tickers):
    fetch = fetch_tw_prices if market == "tw" else fetch_us_prices
    results = []
    for ticker in tickers:
        try:
            rows = fetch(ticker)
            stats = score_ticker(rows)
            if stats:
                results.append({"ticker": ticker, **stats})
        except Exception as exc:  # noqa: BLE001 — 單一個股失敗不影響其他
            print(f"[skip] {ticker}: {exc}")
    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", choices=["tw", "us"], required=True)
    parser.add_argument("--tickers", help="逗號分隔的股票代號，如 2330,2317 或 AAPL,MSFT")
    parser.add_argument("--watchlist", help="每行一個代號的清單檔案")
    parser.add_argument("--top", type=int, default=15)
    args = parser.parse_args()

    if args.watchlist:
        tickers = load_watchlist(args.watchlist)
    elif args.tickers:
        tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    else:
        parser.error("需指定 --tickers 或 --watchlist")

    results = run(args.market, tickers)

    print(f"\n=== {args.market.upper()} 動能候選清單（共 {len(results)} 檔符合條件） ===")
    print("規則：站上 MA20 與 MA60（多頭排列）、近20日量增（vol_ratio>1 越高越好）、RSI<80（未過熱）\n")
    for r in results[: args.top]:
        print(
            f"{r['ticker']:>8}  收盤 {r['close']:>9}  20日漲幅 {r['ret_20d_pct']:>6}%  "
            f"量比 {r['vol_ratio']:>5}  RSI {r['rsi14']:>5}  分數 {r['score']:>6}"
        )

    if not results:
        print("（無符合條件的候選股，或資料抓取全部失敗——請檢查網路/端點狀態）")


if __name__ == "__main__":
    main()
