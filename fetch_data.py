#!/usr/bin/env python3
"""抓取各因子原始值 → 更新 data.json。

設計原則
--------
* 零外部相依：只用 Python 標準函式庫（urllib / json / re），方便在 GitHub Actions
  上直接跑，不必裝套件。
* 永不開天窗：每個來源各自獨立 try/except，任何來源失敗就「沿用 data.json 裡的
  上一次值」並把該因子標記為 stale=True，儀表板會顯示「⚠ 沿用舊值」。
* 沙箱限制：撰寫者的環境連不到這些資料端點，故抓取邏輯未對線上 API 驗證過，
  第一次在本機或 Actions 跑時可能需要微調個別端點。

資料源（誠實清單）
------------------
美股：
  估值   Shiller CAPE      multpl.com 解析（非官方、較脆弱）
  情緒   CNN 恐懼貪婪      production.dataviz.cnn.io（非官方端點，需帶 Referer）
  景氣   殖利率曲線 10Y–2Y  美國財政部每日殖利率 CSV（.gov、免金鑰、10Y-2Y）
  利率   10年期殖利率       美國財政部每日殖利率 CSV（.gov、免金鑰）
                          (原用 FRED fredgraph.csv，從 GitHub runner 會 timeout，改用財政部)
台股：
  資金   外資買賣超        TWSE 開放資料（單日 proxy）
  估值   大盤本益比代理     FinMind TaiwanStockPER, data_id=2330（台積電權值龍頭）
  情緒   融資餘額          FinMind TaiwanStockTotalMarginPurchaseShortSale
  景氣   景氣對策信號       data.gov.tw 開放資料平台「景氣指標及燈號」(dataset 6099, 國發會)，
                          免金鑰、CSV 格式，取對策信號分數欄位 (9-45) 最新一筆。
"""

import csv
import io
import json
import os
import re
import ssl
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA_PATH = Path(__file__).with_name("data.json")
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
_CTX = ssl.create_default_context()

# 用接近真實瀏覽器的標頭，避免 CNN 之類來源以 403/418 擋掉機器人。
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _get_bytes(url, timeout=30, retries=3, headers=None):
    """抓取 URL（原始 bytes），帶瀏覽器標頭 + 重試（指數退避），降低偶發 timeout/擋爬。"""
    merged = dict(BROWSER_HEADERS)
    if headers:
        merged.update(headers)
    last_exc = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=merged)
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001 — 重試後仍失敗才往外丟
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise last_exc


def _get(url, timeout=30, retries=3, headers=None):
    return _get_bytes(url, timeout, retries, headers).decode("utf-8", "replace")


_treasury_cache = {}


def _treasury_latest():
    """美國財政部每日公債殖利率（.gov、無金鑰、比 FRED 穩）。

    回傳最新一筆的 {欄位名: 值(float)}，例如 {'2 Yr': 3.9, '10 Yr': 4.5, ...}。
    年初可能尚無當年資料，會自動退回前一年。模組層級快取避免重複抓取。
    """
    if _treasury_cache:
        return _treasury_cache
    base = ("https://home.treasury.gov/resource-center/data-chart-center/"
            "interest-rates/daily-treasury-rates.csv/{y}/all"
            "?type=daily_treasury_yield_curve&field_tdr_date_value={y}&page&_format=csv")
    year = datetime.now(timezone.utc).year
    for y in (year, year - 1):
        csv_text = _get(base.format(y=y))
        lines = [ln for ln in csv_text.strip().splitlines() if ln]
        if len(lines) < 2:
            continue
        header = [h.strip().strip('"') for h in lines[0].split(",")]
        row = [c.strip().strip('"') for c in lines[1].split(",")]  # 第一列即最新
        out = {}
        for k, v in zip(header, row):
            try:
                out[k] = float(v)
            except ValueError:
                pass  # 日期等非數值欄位略過
        if out:
            _treasury_cache.update(out)
            return _treasury_cache
    raise ValueError("US Treasury CSV 沒有可用數值")


# ---- 美股 ---------------------------------------------------------------------

def fetch_us_cape():
    """Shiller CAPE — 解析 multpl.com（非官方、版面變動就會壞）。"""
    html = _get("https://www.multpl.com/shiller-pe")
    m = re.search(r"Current Shiller PE Ratio[^0-9]*([0-9]+\.[0-9]+)", html, re.I)
    if not m:
        m = re.search(r'id="current"[^>]*>\s*([0-9]+\.[0-9]+)', html)
    if not m:
        raise ValueError("CAPE 解析失敗")
    return float(m.group(1))


def fetch_us_fear_greed():
    """CNN 恐懼貪婪指數 — 非官方端點，需帶 Referer 否則回 418。"""
    raw = _get(
        "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
        headers={"Referer": "https://edition.cnn.com/markets/fear-and-greed"},
    )
    obj = json.loads(raw)
    return round(float(obj["fear_and_greed"]["score"]), 1)


def fetch_us_yield_curve():
    """殖利率曲線 10Y–2Y（%），由美國財政部 2Y/10Y 相減。"""
    y = _treasury_latest()
    return round(y["10 Yr"] - y["2 Yr"], 2)


def fetch_us_10y():
    """美國10年期公債殖利率（%），美國財政部。"""
    return _treasury_latest()["10 Yr"]


# ---- 台股 ---------------------------------------------------------------------

def fetch_tw_foreign_net():
    """外資買賣超（單日 proxy，單位：億元）。TWSE 三大法人買賣金額表。"""
    today = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d")
    url = ("https://www.twse.com.tw/rwd/zh/fund/BFI82U"
           "?response=json&dayDate=" + today + "&type=day")
    obj = json.loads(_get(url))
    if obj.get("stat") != "OK" or not obj.get("data"):
        raise ValueError("TWSE 無當日資料（可能非交易日）")
    total = 0.0
    for row in obj["data"]:
        name = row[0]
        if "外資" in name and "自營" not in name:
            net = float(row[3].replace(",", ""))  # 買賣差額(元)
            total += net
    return round(total / 1e8, 1)  # 元 → 億元


def _finmind(dataset, data_id=None, days=30):
    """FinMind API（無 token 也能用，限流較低；有 FINMIND_TOKEN 走 Bearer）。

    回傳 data 陣列（依日期升冪，最後一筆=最新）。
    """
    end = datetime.now(timezone.utc).astimezone()
    start = end - timedelta(days=days)
    params = {
        "dataset": dataset,
        "start_date": start.strftime("%Y-%m-%d"),
        "end_date": end.strftime("%Y-%m-%d"),
    }
    if data_id:
        params["data_id"] = data_id
    token = os.getenv("FINMIND_TOKEN")
    headers = {"Authorization": "Bearer " + token} if token else None
    obj = json.loads(_get(FINMIND_URL + "?" + urllib.parse.urlencode(params), headers=headers))
    if obj.get("status") != 200:
        raise ValueError("FinMind: " + str(obj.get("msg")))
    return obj.get("data", [])


def fetch_tw_valuation():
    """大盤估值代理：台積電(2330)本益比（佔加權指數約三成的權值龍頭）。"""
    rows = _finmind("TaiwanStockPER", data_id="2330")
    rows = [r for r in rows if r.get("PER") not in (None, "", 0)]
    if not rows:
        raise ValueError("FinMind PER 無資料")
    return round(float(rows[-1]["PER"]), 2)


def fetch_tw_sentiment():
    """情緒代理：整體市場融資餘額金額(億元)。融資高漲=散戶過熱→防禦。"""
    rows = _finmind("TaiwanStockTotalMarginPurchaseShortSale")
    money = [r for r in rows if r.get("name") == "MarginPurchaseMoney"]
    if not money:
        raise ValueError("FinMind 無融資金額資料")
    bal = float(money[-1]["TodayBalance"])
    return round(bal / 1e8, 1)  # 元 → 億元（實測 TodayBalance 單位為元）


def _ym_key(s):
    """「年月」字串 → 可排序的整數 key (西元 YYYYMM)。
    支援 YYYYMM、民國 YYMM/YYYMM(民國年=西元-1911，99 年以前為 4 位數)、
    民國「114年4月」與 YYYY-MM / YYYY/MM。"""
    s = s.strip()
    m = re.fullmatch(r"(\d{4})(\d{2})", s)
    if m and 1911 < int(m.group(1)) < 2200 and 1 <= int(m.group(2)) <= 12:
        return int(s)
    m = re.fullmatch(r"(\d{2,3})(\d{2})", s)
    if m and 1 <= int(m.group(2)) <= 12:
        return (int(m.group(1)) + 1911) * 100 + int(m.group(2))
    m = re.fullmatch(r"(\d{2,3})年(\d{1,2})月?", s)
    if m:
        return (int(m.group(1)) + 1911) * 100 + int(m.group(2))
    m = re.fullmatch(r"(\d{4})[-/](\d{1,2})([-/]\d{1,2})?", s)
    if m:
        return int(m.group(1)) * 100 + int(m.group(2))
    return None


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
                with zipfile.ZipFile(io.BytesIO(_get_bytes(url))) as zf:
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


def fetch_tw_cycle():
    """景氣對策信號：data.gov.tw 開放資料平台「景氣指標及燈號」(dataset 6099, 國發會) 的
    對策信號分數 (9-45)，免金鑰、CSV 格式。

    月頻、月底前後發布且會事後修正；回測時 scores.py 已對因子做月底 resample + lag
    shift，避免用到「當月尚未發布」的數值。
    """
    meta = json.loads(_get("https://data.gov.tw/api/v2/rest/dataset/6099"))
    csv_texts = _ndc_csv_texts(meta)

    last_err = None
    for url, text in csv_texts:
        try:
            rows = list(csv.reader(io.StringIO(text)))
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue
        if len(rows) < 2:
            last_err = ValueError(f"{url} CSV 為空")
            continue

        header, body = rows[0], rows[1:]
        date_idx = next((i for i, c in enumerate(header)
                          if any(k in c for k in ("年月", "日期", "時間", "date", "Date"))), None)
        score_idx = None
        for i, name in enumerate(header):
            if "對策信號" not in name and "燈號" not in name:
                continue
            vals = []
            for row in body:
                if i < len(row):
                    try:
                        vals.append(float(row[i]))
                    except ValueError:
                        pass
            if vals and sum(9 <= v <= 45 for v in vals) / len(vals) > 0.8:
                score_idx = i
                break
        if score_idx is None:
            last_err = ValueError(f"{url} 找不到對策信號欄位，欄位={header}")
            continue

        best_key, best_val = None, None
        for row in body:
            if score_idx >= len(row):
                continue
            try:
                val = float(row[score_idx])
            except ValueError:
                continue
            key = _ym_key(row[date_idx]) if date_idx is not None and date_idx < len(row) else None
            if key is None:
                best_val = val  # 無日期欄時，假設 CSV 依時間升冪排列，取最後一筆有效值
            elif best_key is None or key >= best_key:
                best_key, best_val = key, val
        if best_val is None:
            last_err = ValueError(f"{r['url']} 對策信號欄位無有效數值")
            continue
        return round(best_val, 1)

    raise ValueError(f"dataset 6099 CSV 皆解析失敗: {last_err}")


# ---- 主流程 -------------------------------------------------------------------

FETCHERS = {
    ("US", "valuation"): fetch_us_cape,
    ("US", "sentiment"): fetch_us_fear_greed,
    ("US", "cycle"): fetch_us_yield_curve,
    ("US", "rate"): fetch_us_10y,
    ("TW", "valuation"): fetch_tw_valuation,
    ("TW", "sentiment"): fetch_tw_sentiment,
    ("TW", "cycle"): fetch_tw_cycle,
    ("TW", "rate"): fetch_tw_foreign_net,
}


def main():
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    ok, stale = [], []

    for (mkt, dim), fn in FETCHERS.items():
        factor = data["markets"][mkt]["factors"][dim]
        try:
            value = fn()
            factor["value"] = value
            factor["stale"] = False
            ok.append(f"{mkt}/{dim}={value}")
        except Exception as exc:  # noqa: BLE001 — 任何失敗都沿用舊值
            factor["stale"] = True
            stale.append(f"{mkt}/{dim} ({type(exc).__name__}: {exc})")

    data["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    DATA_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print("✅ 更新:", ", ".join(ok) if ok else "(無)")
    if stale:
        print("⚠ 沿用舊值:", "; ".join(stale))


if __name__ == "__main__":
    main()
