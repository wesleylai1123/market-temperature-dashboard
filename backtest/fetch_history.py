"""抓取回測所需的「因子歷史」與「價格歷史」→ 寫成 backtest/data/*.csv。

需要網路。每個來源各自獨立 try/except，失敗就略過該欄/該檔（scores.py 會自動對
當月有值的因子重新分配權重，所以少一兩個因子仍可回測）。

產出：
  data/us_factors.csv  date,valuation,sentiment,cycle,rate
  data/us_price.csv     date,close                         (SPY)
  data/tw_factors.csv  date,valuation,sentiment[,rate]
  data/tw_price.csv     date,close                         (0050)

用法：
  python fetch_history.py --start 2010-01-01
  FINMIND_TOKEN=xxx python fetch_history.py     # 台股建議設 token 提高限額

來源驗證狀態（沙箱無法實跑，第一次在本機跑會知道哪些要微調）：
  ✅ 已於 Actions/實測確認：FinMind PER/Margin、CNN F&G(帶 Referer)、CAPE(multpl)、US Treasury
  ⚠ 待你本機確認：stooq SPY 價格、FinMind 0050 價格、FinMind 三大法人(外資)資料集名稱
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).with_name("data")
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
_CTX = ssl.create_default_context()
BROWSER = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/json,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _get(url, headers=None, timeout=30, retries=3):
    h = dict(BROWSER)
    if headers:
        h.update(headers)
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i < retries - 1:
                time.sleep(1.5 * (i + 1))
    raise last


# ---- 美股 ---------------------------------------------------------------------

def us_cape():
    """Shiller CAPE 月歷史（multpl by-month 表）。先去 HTML 標籤(含 &nbsp;)再抽 日期+數值。"""
    html = _get("https://www.multpl.com/shiller-pe/table/by-month")
    text = re.sub(r"<[^>]+>", " ", html).replace("\xa0", " ").replace("&nbsp;", " ")
    text = re.sub(r"\s+", " ", text)
    # 容忍 3 字母或全月名、逗號可有可無
    pairs = re.findall(r"([A-Z][a-z]{2,8}\.? \d{1,2},? \d{4})\s+([0-9]{1,3}\.[0-9]+)", text)
    rows = []
    for d, v in pairs:
        for fmt in ("%b %d, %Y", "%b %d %Y", "%B %d, %Y", "%B %d %Y"):
            try:
                rows.append((datetime.strptime(d, fmt), float(v)))
                break
            except ValueError:
                continue
    if not rows:
        raise ValueError(f"CAPE 表解析為 0 筆（text 長度 {len(text)}；樣本: {text[:300]!r}）")
    return pd.Series({d: v for d, v in rows}).sort_index().rename("valuation")


def us_fear_greed():
    """CNN 恐懼貪婪日歷史（fear_and_greed_historical）。"""
    raw = _get("https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
               headers={"Referer": "https://edition.cnn.com/markets/fear-and-greed"})
    data = json.loads(raw)["fear_and_greed_historical"]["data"]
    idx = [pd.to_datetime(p["x"], unit="ms") for p in data]
    return pd.Series([float(p["y"]) for p in data], index=idx).sort_index().rename("sentiment")


def us_treasury(start_year):
    """美國財政部每日 2Y/10Y → cycle(10Y-2Y)、rate(10Y)。逐年抓取。"""
    base = ("https://home.treasury.gov/resource-center/data-chart-center/"
            "interest-rates/daily-treasury-rates.csv/{y}/all"
            "?type=daily_treasury_yield_curve&field_tdr_date_value={y}&page&_format=csv")
    frames = []
    for y in range(start_year, datetime.now(timezone.utc).year + 1):
        try:
            from io import StringIO
            df = pd.read_csv(StringIO(_get(base.format(y=y))))
            df["Date"] = pd.to_datetime(df["Date"])
            frames.append(df.set_index("Date")[["2 Yr", "10 Yr"]])
        except Exception as exc:  # noqa: BLE001
            print(f"  · Treasury {y} 略過: {exc}")
    if not frames:
        raise ValueError("Treasury 無資料")
    t = pd.concat(frames).sort_index()
    out = pd.DataFrame({"cycle": t["10 Yr"] - t["2 Yr"], "rate": t["10 Yr"]})
    return out


def _yahoo_price(symbol, rng="20y"):
    """Yahoo Finance chart API（免金鑰 JSON）→ 日收盤(優先還原權值)。"""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?range={rng}&interval=1d")
    res = json.loads(_get(url))["chart"]["result"][0]
    idx = pd.to_datetime(res["timestamp"], unit="s").normalize()
    ind = res["indicators"]
    closes = (ind.get("adjclose", [{}])[0].get("adjclose")
              or ind["quote"][0]["close"])
    return pd.Series(closes, index=idx).dropna().rename("close")


def _stooq_price(symbol):
    from io import StringIO
    df = pd.read_csv(StringIO(_get(f"https://stooq.com/q/d/l/?s={symbol}&i=d")))
    if "Date" not in df.columns:
        raise ValueError(f"stooq 回傳非預期內容（欄位 {list(df.columns)}，可能限流）")
    df["date"] = pd.to_datetime(df["Date"])
    return df.set_index("date")["Close"].rename("close")


def us_price():
    """SPY 日收盤：Yahoo chart 為主、stooq 備援（皆免金鑰）。"""
    try:
        return _yahoo_price("SPY")
    except Exception as exc:  # noqa: BLE001
        print(f"  · Yahoo SPY 失敗改用 stooq: {exc}")
        return _stooq_price("spy.us")


# ---- 台股（FinMind）-----------------------------------------------------------

def _finmind(dataset, start, data_id=None):
    params = {"dataset": dataset, "start_date": start}
    if data_id:
        params["data_id"] = data_id
    token = os.getenv("FINMIND_TOKEN")
    headers = {"Authorization": "Bearer " + token} if token else None
    obj = json.loads(_get(FINMIND_URL + "?" + urllib.parse.urlencode(params), headers=headers))
    if obj.get("status") != 200:
        raise ValueError("FinMind: " + str(obj.get("msg")))
    return pd.DataFrame(obj.get("data", []))


def tw_valuation(start):
    df = _finmind("TaiwanStockPER", start, data_id="2330")
    df["date"] = pd.to_datetime(df["date"])
    return pd.to_numeric(df.set_index("date")["PER"], errors="coerce").rename("valuation")


def tw_sentiment(start):
    df = _finmind("TaiwanStockTotalMarginPurchaseShortSale", start)
    df = df[df["name"] == "MarginPurchaseMoney"].copy()
    df["date"] = pd.to_datetime(df["date"])
    return (pd.to_numeric(df.set_index("date")["TodayBalance"], errors="coerce") / 1e8).rename("sentiment")


def tw_price(start):
    df = _finmind("TaiwanStockPrice", start, data_id="0050")
    df["date"] = pd.to_datetime(df["date"])
    return pd.to_numeric(df.set_index("date")["close"], errors="coerce").rename("close")


def tw_foreign(start):
    """外資買賣超(億)日歷史。資料集名稱待本機確認；失敗則略過此因子。"""
    df = _finmind("TaiwanStockTotalInstitutionalInvestors", start)
    fore = df[df["name"].astype(str).str.contains("Foreign")].copy()
    fore["date"] = pd.to_datetime(fore["date"])
    net = (pd.to_numeric(fore["buy"], errors="coerce") - pd.to_numeric(fore["sell"], errors="coerce"))
    g = net.groupby(fore["date"]).sum() / 1e8
    return g.rename("rate")


# ---- 組裝 ---------------------------------------------------------------------

def _try(label, fn):
    try:
        s = fn()
        print(f"  ✅ {label}: {len(s)} 筆")
        return s
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠ {label} 略過: {type(exc).__name__}: {exc}")
        return None


def build(start: str):
    DATA_DIR.mkdir(exist_ok=True)
    start_year = int(start[:4])
    start_ts = pd.Timestamp(start)

    print("美股因子/價格:")
    us_cols = [s for s in (
        _try("CAPE", us_cape),
        _try("Fear&Greed", us_fear_greed),
    ) if s is not None]
    tre = _try("Treasury 2Y/10Y", lambda: us_treasury(start_year))
    if tre is not None:
        us_cols += [tre["cycle"], tre["rate"]]
    if us_cols:
        us = pd.concat(us_cols, axis=1).sort_index()
        us = us[us.index >= start_ts]
        us.index.name = "date"
        us.to_csv(DATA_DIR / "us_factors.csv")
        print(f"  → us_factors.csv ({len(us)} 列, 欄位 {list(us.columns)})")
    sp = _try("SPY price", us_price)
    if sp is not None:
        sp = sp[sp.index >= start_ts]
        sp.to_frame().to_csv(DATA_DIR / "us_price.csv", index_label="date")
        print(f"  → us_price.csv ({len(sp)} 列)")

    print("台股因子/價格:")
    tw_cols = [s for s in (
        _try("2330 PER", lambda: tw_valuation(start)),
        _try("融資餘額", lambda: tw_sentiment(start)),
        _try("外資買賣超", lambda: tw_foreign(start)),
    ) if s is not None]
    if tw_cols:
        tw = pd.concat(tw_cols, axis=1).sort_index()
        tw = tw[tw.index >= start_ts]
        tw.index.name = "date"
        tw.to_csv(DATA_DIR / "tw_factors.csv")
        print(f"  → tw_factors.csv ({len(tw)} 列, 欄位 {list(tw.columns)})")
    tp = _try("0050 price", lambda: tw_price(start))
    if tp is not None:
        tp = tp[tp.index >= start_ts]
        tp.to_frame().to_csv(DATA_DIR / "tw_price.csv", index_label="date")
        print(f"  → tw_price.csv ({len(tp)} 列)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2010-01-01", help="歷史起始日 YYYY-MM-DD")
    args = ap.parse_args()
    build(args.start)


if __name__ == "__main__":
    main()
