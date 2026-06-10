"""產生 DEMO 合成資料，用於本地測試系統流程（非真實市場資料）。

執行後會在 backtest/data/ 寫入：
  - price_SPY.csv, price_QQQ.csv, price_XLK.csv, price_XLU.csv, price_XLV.csv
  - price_0050.csv, price_0056.csv
  - us_factors.csv, tw_factors.csv（合成因子）
  - price_<成長股候選代號>.csv（10 檔 TW + 10 檔 US，含 start 前 15 個月歷史）
  - revenue_<TW代號>.csv（10 檔 TW 候選股合成月營收，含 start 前 27 個月歷史）

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

# 成長股候選池（同 growth_agent.CANDIDATES）：價格走勢依「成長強弱」分散，
# 讓 Top-N 動能/營收YoY 排名與等權重池子有顯著差異，便於驗證選股是否有 alpha。
TW_GROWTH = {
    "2330": {"mu": 0.18, "sigma": 0.22, "start": 1000.0, "rev_base": 2200.0, "rev_growth": 0.28, "rev_sigma": 0.05},
    "2454": {"mu": 0.10, "sigma": 0.25, "start": 1100.0, "rev_base": 500.0,  "rev_growth": 0.15, "rev_sigma": 0.06},
    "3034": {"mu": 0.15, "sigma": 0.28, "start": 600.0,  "rev_base": 80.0,   "rev_growth": 0.22, "rev_sigma": 0.08},
    "2379": {"mu": 0.08, "sigma": 0.24, "start": 500.0,  "rev_base": 60.0,   "rev_growth": 0.10, "rev_sigma": 0.07},
    "3008": {"mu": 0.02, "sigma": 0.20, "start": 2500.0, "rev_base": 90.0,   "rev_growth": -0.05, "rev_sigma": 0.09},
    "2382": {"mu": 0.12, "sigma": 0.23, "start": 280.0,  "rev_base": 1300.0, "rev_growth": 0.18, "rev_sigma": 0.06},
    "2317": {"mu": 0.06, "sigma": 0.18, "start": 200.0,  "rev_base": 5800.0, "rev_growth": 0.06, "rev_sigma": 0.05},
    "6669": {"mu": 0.30, "sigma": 0.35, "start": 1800.0, "rev_base": 250.0,  "rev_growth": 0.45, "rev_sigma": 0.10},
    "3231": {"mu": 0.25, "sigma": 0.30, "start": 100.0,  "rev_base": 700.0,  "rev_growth": 0.35, "rev_sigma": 0.08},
    "2308": {"mu": 0.07, "sigma": 0.16, "start": 400.0,  "rev_base": 280.0,  "rev_growth": 0.08, "rev_sigma": 0.05},
}

US_GROWTH = {
    "AAPL":  {"mu": 0.12, "sigma": 0.20, "start": 230.0},
    "MSFT":  {"mu": 0.18, "sigma": 0.18, "start": 430.0},
    "NVDA":  {"mu": 0.45, "sigma": 0.40, "start": 140.0},
    "GOOGL": {"mu": 0.15, "sigma": 0.22, "start": 175.0},
    "AMZN":  {"mu": 0.14, "sigma": 0.24, "start": 200.0},
    "META":  {"mu": 0.30, "sigma": 0.28, "start": 600.0},
    "TSLA":  {"mu": 0.05, "sigma": 0.45, "start": 350.0},
    "AVGO":  {"mu": 0.28, "sigma": 0.30, "start": 230.0},
    "AMD":   {"mu": 0.18, "sigma": 0.35, "start": 150.0},
    "NFLX":  {"mu": 0.10, "sigma": 0.25, "start": 900.0},
}

GROWTH_LOOKBACK_MONTHS = 15          # 12-1 動能 / 營收 YoY 需要的價格歷史
GROWTH_REVENUE_LOOKBACK_MONTHS = 27  # 12 個月 YoY + lag 緩衝
GROWTH_N_DAYS = 480                  # 涵蓋 lookback + 之後約 7 個月的交易日

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

    build_growth(start)
    print(f"\n✅ DEMO 資料已寫入 {DATA_DIR}  ⚠ 非真實市場資料")


def build_growth(start: str) -> None:
    """成長股候選池價格 + 台股候選股合成月營收（供 growth_agent 回測）。"""
    growth_start = pd.Timestamp(start) - pd.DateOffset(months=GROWTH_LOOKBACK_MONTHS)
    dates = _biz_dates(growth_start.strftime("%Y-%m-%d"), GROWTH_N_DAYS)
    n = len(dates)

    print("產生成長股候選池價格…")
    for sym, cfg in {**TW_GROWTH, **US_GROWTH}.items():
        prices = _gbm_prices(n, cfg["mu"], cfg["sigma"], cfg["start"])
        df = pd.DataFrame({"close": prices}, index=dates)
        df.index.name = "date"
        path = DATA_DIR / f"price_{sym}.csv"
        df.to_csv(path)
        print(f"  → {path.name}  ({n} 列, 起始 {growth_start.date()})")

    print("產生台股候選股合成月營收…")
    rev_start = pd.Timestamp(start) - pd.DateOffset(months=GROWTH_REVENUE_LOOKBACK_MONTHS)
    rev_end = pd.Timestamp(start) + pd.DateOffset(months=7)
    months = pd.date_range(rev_start, rev_end, freq="ME")
    n_m = len(months)
    for sym, cfg in TW_GROWTH.items():
        monthly_growth = (1 + cfg["rev_growth"]) ** (1 / 12)
        trend = cfg["rev_base"] * monthly_growth ** np.arange(n_m)
        noise = 1 + np.random.normal(0, cfg["rev_sigma"], n_m)
        revenue = np.clip(trend * noise, 1.0, None)
        df = pd.DataFrame({"revenue": revenue}, index=months)
        df.index.name = "date"
        path = DATA_DIR / f"revenue_{sym}.csv"
        df.to_csv(path)
        print(f"  → {path.name}  ({n_m} 月, YoY={cfg['rev_growth']*100:+.0f}%)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-12-01")
    args = ap.parse_args()
    build(args.start)


if __name__ == "__main__":
    main()
