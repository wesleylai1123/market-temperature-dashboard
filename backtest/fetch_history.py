"""抓取回測所需的「因子歷史」與「價格歷史」→ 寫成 backtest/data/*.csv。

需要網路。每個來源各自獨立 try/except，失敗就略過該欄/該檔（scores.py 會自動對
當月有值的因子重新分配權重，所以少一兩個因子仍可回測）。

產出：
  data/us_factors.csv  date,valuation,sentiment,cycle,rate
  data/us_price.csv     date,close                         (SPY)
  data/tw_factors.csv  date,valuation,sentiment,cycle[,rate]
  data/tw_price.csv     date,close                         (0050)

用法：
  python fetch_history.py --start 2010-01-01
  FINMIND_TOKEN=xxx python fetch_history.py     # 台股建議設 token 提高限額

來源驗證狀態（已於 GitHub Actions runner 實跑確認）：
  ✅ 可抓：CNN F&G(帶 Referer，約近1年)、US Treasury 2Y/10Y、Yahoo SPY、
          FinMind 2330 PER / 整體融資 / 三大法人(外資) / 0050 價格、
          data.gov.tw 景氣指標及燈號(dataset 6099, 國發會, 景氣對策信號)
  ⚠ CAPE 未解：multpl 全頁 JS(AJAX 載入)，靜態抓不到；nasdaq/quandl 需金鑰。
     → 待補：改抓 Shiller 官方 ie_data.xls (需 xlrd，月頻 1871 起)。目前美股缺估值因子，
       回測自動以 利率+曲線+情緒 重分配權重(同台股缺景氣的處理)。
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


def _get_bytes(url, headers=None, timeout=30, retries=3):
    h = dict(BROWSER)
    if headers:
        h.update(headers)
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                return r.read()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i < retries - 1:
                time.sleep(1.5 * (i + 1))
    raise last


def _get(url, headers=None, timeout=30, retries=3):
    return _get_bytes(url, headers, timeout, retries).decode("utf-8", "replace")


# ---- 美股 ---------------------------------------------------------------------

def us_cape():
    """Shiller CAPE 月歷史。multpl 圖表資料嵌在頁面 JS 的 [epoch_ms, value] 陣列裡。"""
    html = _get("https://www.multpl.com/shiller-pe")

    # 策略 A：JS 圖表序列 [epoch_ms, value]（epoch 可為負，代表 1970 前）
    pairs = re.findall(r"\[\s*(-?\d{11,14})\s*,\s*([0-9]{1,3}(?:\.[0-9]+)?)\s*\]", html)
    rows = []
    for ms, v in pairs:
        ms, v = int(ms), float(v)
        if 1.0 <= v <= 100.0:  # 過濾非 CAPE 的數對
            rows.append((pd.to_datetime(ms, unit="ms"), v))

    # 策略 B：純文字表格（去標籤）備援
    if not rows:
        text = re.sub(r"<[^>]+>", " ", _get("https://www.multpl.com/shiller-pe/table/by-month"))
        text = text.replace("\xa0", " ").replace("&nbsp;", " ")
        text = re.sub(r"\s+", " ", text)
        for d, v in re.findall(r"([A-Z][a-z]{2,8}\.? \d{1,2},? \d{4})\s+([0-9]{1,3}\.[0-9]+)", text):
            for fmt in ("%b %d, %Y", "%b %d %Y", "%B %d, %Y", "%B %d %Y"):
                try:
                    rows.append((datetime.strptime(d, fmt), float(v)))
                    break
                except ValueError:
                    continue

    if not rows:
        raise ValueError("CAPE 解析為 0 筆（JS 與表格兩法皆失敗，版面可能變動）")
    return pd.Series(dict(rows)).sort_index().rename("valuation")


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


def _parse_ndc_date(series):
    """國發會/data.gov.tw「年月」常見格式：YYYYMM、民國 YYMM/YYYMM(民國年=西元-1911，
    民國 99 年以前只有 4 位數，同一序列可能混雜 4/5 位)、民國「114年4月」或一般日期字串。
    逐元素解析，無法解析者轉 NaT 由呼叫端 dropna。"""
    def one(v):
        v = str(v).strip()
        m = re.fullmatch(r"(\d{4})(\d{2})", v)
        if m and 1911 < int(m.group(1)) < 2200 and 1 <= int(m.group(2)) <= 12:
            return f"{m.group(1)}-{m.group(2)}-01"
        m = re.fullmatch(r"(\d{2,3})(\d{2})", v)
        if m and 1 <= int(m.group(2)) <= 12:
            return f"{int(m.group(1)) + 1911}-{m.group(2)}-01"
        m = re.fullmatch(r"(\d{2,3})年(\d{1,2})月?", v)
        if m:
            return f"{int(m.group(1)) + 1911}-{int(m.group(2)):02d}-01"
        return v
    return pd.to_datetime(series.map(one), errors="coerce")


def _decode_csv_bytes(b):
    """NDC/data.gov.tw 的 CSV 可能是 UTF-8(含 BOM) 或 Big5/CP950 編碼。"""
    for enc in ("utf-8-sig", "cp950"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace")


def _ndc_csv_texts(meta):
    """data.gov.tw dataset API 的資源清單欄位在不同資料集/版本間不一致：
    可能是 CKAN 風格 result.resources（欄位 format/url），
    也可能是 DCAT 風格 result.distribution（欄位 resourceFormat/resourceDownloadUrl）。
    資源本身可能是 CSV，也可能是含多個 CSV 的 ZIP（dataset 6099 實測為 ZIP）。
    相容以上情況，回傳 [(label, csv_text), ...]。
    """
    import zipfile
    from io import BytesIO

    result = meta.get("result")
    items = None
    if isinstance(result, list):
        items = result
    elif isinstance(result, dict):
        for key in ("resources", "distribution", "distributions"):
            v = result.get(key)
            if isinstance(v, list):
                items = v
                break
    if items is None:
        detail = list(result.keys()) if isinstance(result, dict) else type(result).__name__
        raise ValueError(f"dataset 6099 回傳格式非預期，result 結構={detail}")

    out, notes = [], []
    for r in items:
        fmt = str(r.get("format") or r.get("resourceFormat") or "").upper()
        url = r.get("url") or r.get("resourceDownloadUrl") or r.get("downloadURL") or r.get("accessURL")
        if not url:
            continue
        try:
            if fmt == "CSV" or str(url).lower().endswith(".csv"):
                out.append((url, _decode_csv_bytes(_get_bytes(url))))
            elif fmt == "ZIP" or ".zip" in str(url).lower():
                with zipfile.ZipFile(BytesIO(_get_bytes(url))) as zf:
                    members = zf.namelist()
                    csv_members = [m for m in members if m.lower().endswith(".csv")]
                    if not csv_members:
                        notes.append(f"{url} ZIP 內無 CSV，成員={members}")
                    for m in csv_members:
                        out.append((f"{url}!{m}", _decode_csv_bytes(zf.read(m))))
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{url} 下載/解壓失敗: {type(exc).__name__}: {exc}")
    if not out:
        names = [(r.get("name") or r.get("resourceDescription"),
                   r.get("format") or r.get("resourceFormat")) for r in items]
        raise ValueError(f"dataset 6099 無可用 CSV，resources={names}；{'; '.join(notes)}")
    return out


def tw_cycle(start):
    """景氣對策信號(9-45)月頻歷史。data.gov.tw 開放資料平台「景氣指標及燈號」(dataset 6099, 國發會)，免金鑰。

    CSV 欄位名稱可能隨版本調整，故以關鍵字（含「對策信號」或「燈號」）＋數值範圍(9-45)
    雙重判斷找出對策信號分數欄位，並嘗試多個 CSV 資源直到成功解析。
    """
    from io import StringIO

    meta = json.loads(_get("https://data.gov.tw/api/v2/rest/dataset/6099"))
    csv_texts = _ndc_csv_texts(meta)

    last_err: Exception | None = None
    for url, text in csv_texts:
        try:
            df = pd.read_csv(StringIO(text))
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue

        date_col = next((c for c in df.columns
                          if any(k in str(c) for k in ("年月", "日期", "時間", "date", "Date"))), None)
        score_col = None
        for c in df.columns:
            if "對策信號" not in str(c) and "燈號" not in str(c):
                continue
            vals = pd.to_numeric(df[c], errors="coerce").dropna()
            if len(vals) and vals.between(9, 45).mean() > 0.8:
                score_col = c
                break
        if date_col is None or score_col is None:
            last_err = ValueError(f"{url} 找不到日期/對策信號欄位，欄位={list(df.columns)}")
            continue

        idx = _parse_ndc_date(df[date_col])
        out = pd.Series(pd.to_numeric(df[score_col], errors="coerce").values, index=idx)
        out = out.dropna().sort_index()
        out = out[out.index >= pd.Timestamp(start)]
        if out.empty:
            last_err = ValueError(f"{url} 解析後於 {start} 之後無資料")
            continue
        return out.rename("cycle")

    raise ValueError(f"dataset 6099 CSV 皆解析失敗: {last_err}")


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
        us_cols = [s[~s.index.duplicated(keep="last")] for s in us_cols]
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
        _try("景氣對策信號", lambda: tw_cycle(start)),
        _try("外資買賣超", lambda: tw_foreign(start)),
    ) if s is not None]
    if tw_cols:
        tw_cols = [s[~s.index.duplicated(keep="last")] for s in tw_cols]
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
