"""產生 DEMO 合成資料，用於本地測試系統流程（非真實市場資料）。

執行後會在 backtest/data/ 寫入：
  - price_SPY.csv, price_QQQ.csv, price_XLK.csv, price_XLU.csv, price_XLV.csv
  - price_0050.csv, price_0056.csv
  - us_factors.csv, tw_factors.csv（合成因子）

用法：
    python make_demo_data.py --start 2025-12-01
    python make_demo_data.py --start 2010-01-01   # 長期 demo
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).with_name("data")
ROOT = Path(__file__).resolve().parents[1]

# 各標的年化波動 / 年化報酬率（歷史估計，合成用）
US_ASSETS = {
    "SPY":  {"mu": 0.10, "sigma": 0.15, "start": 510.0},
    "QQQ":  {"mu": 0.12, "sigma": 0.20, "start": 490.0},
    "XLK":  {"mu": 0.12, "sigma": 0.22, "start": 230.0},
    "XLU":  {"mu": 0.05, "sigma": 0.12, "start": 72.0},
    "XLV":  {"mu": 0.06, "sigma": 0.13, "start": 162.0},
    "MTUM": {"mu": 0.11, "sigma": 0.18, "start": 225.0},
    "QUAL": {"mu": 0.09, "sigma": 0.14, "start": 175.0},
    "USMV": {"mu": 0.07, "sigma": 0.11, "start": 95.0},
    "SPLV": {"mu": 0.06, "sigma": 0.11, "start": 74.0},
}

TW_ASSETS = {
    "0050": {"mu": 0.09, "sigma": 0.18, "start": 175.0},
    "0056": {"mu": 0.06, "sigma": 0.12, "start": 42.0},
}

# 因子初始值（參考 data.json 當前值）
US_FACTOR_SEED = {
    "valuation": 41.67,  # CAPE
    "sentiment": 40.0,   # CNN F&G
    "cycle":      0.41,  # 10Y-2Y
    "rate":       4.56,  # 10Y yield
}
TW_FACTOR_SEED = {
    "valuation": 30.86,  # 2330 PER
    "sentiment": 5510.0, # 融資餘額(億)
    "rate":      0.0,    # 外資買賣超
}

np.random.seed(42)


def _gbm_prices(n_days: int, mu: float, sigma: float, start: float) -> np.ndarray:
    """幾何布朗運動日收盤序列。"""
    dt = 1 / 252
    log_ret = (mu - 0.5 * sigma ** 2) * dt + sigma * np.sqrt(dt) * np.random.randn(n_days)
    return start * np.exp(np.cumsum(log_ret))


def _biz_dates(start: str, n: int) -> pd.DatetimeIndex:
    return pd.bdate_range(start=start, periods=n)


def build(start: str) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    dates = _biz_dates(start, 400)   # 400 交易日足夠任何回測期間
    n = len(dates)

    # 美股標的
    print("產生美股標的…")
    for sym, cfg in US_ASSETS.items():
        prices = _gbm_prices(n, cfg["mu"], cfg["sigma"], cfg["start"])
        df = pd.DataFrame({"close": prices}, index=dates)
        df.index.name = "date"
        path = DATA_DIR / f"price_{sym}.csv"
        df.to_csv(path)
        print(f"  → {path.name}  ({n} 列, 起始 {cfg['start']:.2f})")

    # 台股標的
    print("產生台股標的…")
    for sym, cfg in TW_ASSETS.items():
        prices = _gbm_prices(n, cfg["mu"], cfg["sigma"], cfg["start"])
        df = pd.DataFrame({"close": prices}, index=dates)
        df.index.name = "date"
        path = DATA_DIR / f"price_{sym.replace('/', '_')}.csv"
        df.to_csv(path)
        print(f"  → {path.name}")

    # 美股因子（日頻，合成緩慢漂移）
    print("產生美股因子…")
    us_rows = {"valuation": [], "sentiment": [], "cycle": [], "rate": []}
    v = dict(US_FACTOR_SEED)
    for _ in range(n):
        # 緩慢均值回歸 + 雜訊
        v["valuation"] += np.random.normal(0, 0.05)
        v["sentiment"] += np.random.normal(0, 0.8)
        v["sentiment"] = np.clip(v["sentiment"], 0, 100)
        v["cycle"] += np.random.normal(0, 0.02)
        v["rate"] += np.random.normal(0, 0.02)
        for k in us_rows:
            us_rows[k].append(v[k])
    us_df = pd.DataFrame(us_rows, index=dates)
    us_df.index.name = "date"
    us_df.to_csv(DATA_DIR / "us_factors.csv")
    print(f"  → us_factors.csv  ({n} 列)")

    # 台股因子
    print("產生台股因子…")
    tw_rows = {"valuation": [], "sentiment": [], "rate": []}
    v = dict(TW_FACTOR_SEED)
    for _ in range(n):
        v["valuation"] += np.random.normal(0, 0.05)
        v["sentiment"] += np.random.normal(0, 30)
        v["sentiment"] = max(2000, min(8000, v["sentiment"]))
        v["rate"] += np.random.normal(0, 10)
        for k in tw_rows:
            tw_rows[k].append(v[k])
    tw_df = pd.DataFrame(tw_rows, index=dates)
    tw_df.index.name = "date"
    tw_df.to_csv(DATA_DIR / "tw_factors.csv")
    print(f"  → tw_factors.csv  ({n} 列)")
    print(f"\n✅ DEMO 資料已寫入 {DATA_DIR}  ⚠ 非真實市場資料")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-12-01")
    args = ap.parse_args()
    build(args.start)


if __name__ == "__main__":
    main()
