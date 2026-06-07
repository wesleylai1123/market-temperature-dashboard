"""用 backtrader 回測「市場溫度分數」訊號 vs 買進持有。

策略：每月初依「上月底已知的合成分數」決定股票目標權重 = 分數/100（0–100% 在股票、
其餘現金），用 order_target_percent 月度再平衡。對照組為 100% 買進持有同一標的。

用法：
    python run_backtest.py --market US --price data/us_price.csv --factors data/us_factors.csv
    python run_backtest.py --market TW --price data/tw_price.csv --factors data/tw_factors.csv

價格 CSV 需有欄位 date,close；因子 CSV 需有 date 及各因子欄位(valuation/sentiment/cycle/rate)。
"""

from __future__ import annotations

import argparse
from pathlib import Path

import backtrader as bt
import numpy as np
import pandas as pd

from scores import load_config, market_target_weight

INIT_CASH = 1_000_000.0
ANN = 252  # 日頻年化因子


# ---- 記錄每日權益的分析器 ------------------------------------------------------
class EquityRecorder(bt.Analyzer):
    def start(self):
        self.dates, self.values = [], []

    def next(self):
        self.dates.append(self.strategy.datetime.date(0))
        self.values.append(self.strategy.broker.getvalue())

    def get_analysis(self):
        return pd.Series(self.values, index=pd.to_datetime(self.dates))


# ---- 月度依分數再平衡的策略 ----------------------------------------------------
class ScoreAllocation(bt.Strategy):
    params = dict(target_by_date={})

    def __init__(self):
        self._last_ym = None

    def next(self):
        dt = self.data.datetime.date(0)
        ym = (dt.year, dt.month)
        if ym == self._last_ym:
            return
        self._last_ym = ym
        w = self.p.target_by_date.get(pd.Timestamp(dt))
        if w is not None and not np.isnan(w):
            self.order_target_percent(target=float(w))


def _price_feed(price_df: pd.DataFrame) -> bt.feeds.PandasData:
    df = price_df.copy()
    df.index = pd.to_datetime(df["date"])
    close = pd.to_numeric(df["close"], errors="coerce")
    feed_df = pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close,
         "volume": 0.0, "openinterest": 0.0},
        index=df.index,
    ).dropna().sort_index()
    return bt.feeds.PandasData(dataname=feed_df), feed_df


def _kpis(equity: pd.Series) -> dict:
    equity = equity.dropna()
    ret = equity.pct_change().dropna()
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1 if years > 0 else float("nan")
    sharpe = (ret.mean() / ret.std() * np.sqrt(ANN)) if ret.std() > 0 else float("nan")
    roll_max = equity.cummax()
    max_dd = ((equity - roll_max) / roll_max).min()
    return {"CAGR": cagr, "Sharpe": sharpe, "MaxDD": max_dd,
            "Final": equity.iloc[-1], "Years": years}


def _run_strategy(feed, target_by_date) -> pd.Series:
    cerebro = bt.Cerebro()
    cerebro.adddata(feed)
    cerebro.broker.setcash(INIT_CASH)
    cerebro.addstrategy(ScoreAllocation, target_by_date=target_by_date)
    cerebro.addanalyzer(EquityRecorder, _name="equity")
    strat = cerebro.run()[0]
    return strat.analyzers.equity.get_analysis()


def run(market: str, price_csv: str, factors_csv: str, lag_months: int = 1):
    cfg = load_config()
    price_df = pd.read_csv(price_csv)
    factors_df = pd.read_csv(factors_csv).set_index("date")

    target = market_target_weight(market, factors_df, cfg, lag_months=lag_months)

    feed, feed_df = _price_feed(price_df)
    # 月底目標權重 → 攤到每個交易日(ffill)，策略月初讀當日值
    daily_target = target.reindex(feed_df.index, method="ffill")
    target_by_date = {ts: v for ts, v in daily_target.items()}

    strat_equity = _run_strategy(feed, target_by_date)

    # 買進持有對照（解析計算，精確）
    close = feed_df["close"]
    bh_equity = INIT_CASH * close / close.iloc[0]
    # 對齊回測有效區間（分數需暖身，策略前期可能未進場）
    common = strat_equity.index.intersection(bh_equity.index)
    strat_equity, bh_equity = strat_equity.loc[common], bh_equity.loc[common]

    sk, bk = _kpis(strat_equity), _kpis(bh_equity)

    print(f"\n=== {market} 市場溫度分數策略 vs 買進持有 ===")
    print(f"期間 {strat_equity.index[0].date()} ~ {strat_equity.index[-1].date()}  ({sk['Years']:.1f} 年)")
    print(f"{'指標':<10}{'分數策略':>16}{'買進持有':>16}")
    print(f"{'CAGR':<10}{sk['CAGR']*100:>15.2f}%{bk['CAGR']*100:>15.2f}%")
    print(f"{'Sharpe':<10}{sk['Sharpe']:>16.2f}{bk['Sharpe']:>16.2f}")
    print(f"{'MaxDD':<10}{sk['MaxDD']*100:>15.2f}%{bk['MaxDD']*100:>15.2f}%")
    print(f"{'期末資產':<10}{sk['Final']:>16,.0f}{bk['Final']:>16,.0f}")
    print("\n註：分數策略以現金降低曝險，預期 CAGR 多半略低於買進持有，"
          "但若 MaxDD 明顯較小、Sharpe 較高，代表這套燈號對控制風險有效。")
    return sk, bk


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", required=True, choices=["US", "TW"])
    ap.add_argument("--price", required=True)
    ap.add_argument("--factors", required=True)
    ap.add_argument("--lag-months", type=int, default=1)
    args = ap.parse_args()
    run(args.market, args.price, args.factors, args.lag_months)


if __name__ == "__main__":
    main()
