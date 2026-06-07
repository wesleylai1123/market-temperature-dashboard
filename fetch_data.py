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
  情緒   CNN 恐懼貪婪      production.dataviz.cnn.io（非官方端點）
  景氣   殖利率曲線 10Y–2Y  FRED fredgraph.csv?id=T10Y2Y（免金鑰、最穩）
  利率   10年期殖利率       FRED fredgraph.csv?id=DGS10（免金鑰、最穩）
台股：
  資金   外資買賣超        TWSE 開放資料（單日 proxy）
  估值/情緒/景氣           尚未實作 → NotImplementedError → 沿用舊值
                          補齊最快的路：接 FinMind（免費 token）。
"""

import json
import re
import ssl
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DATA_PATH = Path(__file__).with_name("data.json")
UA = "Mozilla/5.0 (market-temperature-dashboard fetch_data.py)"
_CTX = ssl.create_default_context()


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as resp:
        return resp.read().decode("utf-8", "replace")


def _last_csv_value(csv_text):
    """FRED fredgraph.csv：取最後一筆非缺值（缺值以 '.' 表示）。"""
    rows = [r for r in csv_text.strip().splitlines() if r]
    for row in reversed(rows[1:]):  # 跳過表頭
        parts = row.split(",")
        if len(parts) >= 2 and parts[1].strip() not in (".", "", "NaN"):
            return float(parts[1])
    raise ValueError("FRED CSV 沒有可用數值")


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
    """CNN 恐懼貪婪指數 — 非官方資料端點。"""
    raw = _get("https://production.dataviz.cnn.io/index/fearandgreed/graphdata")
    obj = json.loads(raw)
    return float(obj["fear_and_greed"]["score"])


def fetch_fred(series_id):
    csv = _get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=" + series_id)
    return _last_csv_value(csv)


def fetch_us_yield_curve():
    return fetch_fred("T10Y2Y")


def fetch_us_10y():
    return fetch_fred("DGS10")


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


def fetch_tw_valuation():
    raise NotImplementedError("台股大盤本益比尚未接線（建議用 FinMind）")


def fetch_tw_sentiment():
    raise NotImplementedError("台股情緒尚未接線（建議用 FinMind 融資餘額/波動）")


def fetch_tw_cycle():
    raise NotImplementedError("國發會景氣對策信號尚未接線（建議用 FinMind / 國發會）")


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
