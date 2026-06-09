"""用 backtrader 回測「市場溫度分數」訊號 vs 買進持有。

策略：每月初依「上月底已知的合成分數」決定股票目標權重 = 分數/100（0–100% 在股票、
其餘現金），用 order_target_percent 月度再平衡。對照組為 100% 買進持有同一標的。

用法：
    python run_backtest.py --market US --price data/us_price.csv --factors data/us_factors.csv
    python run_backtest.py --market TW --price data/tw_price.csv --factors data/tw_factors.csv
    python run_backtest.py --market US --price data/us_price.csv --factors data/us_factors.csv \\
        --output-json results/us_results.json

價格 CSV 需有欄位 date,close；因子 CSV 需有 date 及各因子欄位(valuation/sentiment/cycle/rate)。
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import backtrader as bt
import numpy as np
import pandas as pd

from scores import load_config, market_target_weight

INIT_CASH = 1_000_000.0
ANN = 252  # 日頻年化因子
TRADE_THRESHOLD = 0.05  # 目標權重變化超過此值才記錄為一筆交易


# ── 分析器：記錄每日權益 ─────────────────────────────────────────────────────

class EquityRecorder(bt.Analyzer):
    def start(self):
        self.dates, self.values = [], []

    def next(self):
        self.dates.append(self.strategy.datetime.date(0))
        self.values.append(self.strategy.broker.getvalue())

    def get_analysis(self) -> pd.Series:
        return pd.Series(self.values, index=pd.to_datetime(self.dates))


# ── 策略：月度依分數再平衡 ───────────────────────────────────────────────────

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


# ── 輔助函式 ─────────────────────────────────────────────────────────────────

def _price_feed(price_df: pd.DataFrame) -> tuple[bt.feeds.PandasData, pd.DataFrame]:
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
    return {
        "cagr": round(float(cagr), 4),
        "sharpe": round(float(sharpe), 3),
        "max_dd": round(float(max_dd), 4),
        "final": round(float(equity.iloc[-1]), 2),
        "years": round(years, 1),
    }


def _run_strategy(feed: bt.feeds.PandasData, target_by_date: dict) -> pd.Series:
    cerebro = bt.Cerebro()
    cerebro.adddata(feed)
    cerebro.broker.setcash(INIT_CASH)
    cerebro.addstrategy(ScoreAllocation, target_by_date=target_by_date)
    cerebro.addanalyzer(EquityRecorder, _name="equity")
    strat = cerebro.run()[0]
    return strat.analyzers.equity.get_analysis()


def _monthly_equity(equity: pd.Series) -> tuple[list[str], list[float]]:
    """月底採樣，標準化起始值=100，回傳 dates 與 values。"""
    monthly = equity.resample("ME").last().dropna()
    if monthly.empty:
        return [], []
    norm = (monthly / monthly.iloc[0] * 100.0).round(2)
    return (
        [ts.strftime("%Y-%m-%d") for ts in norm.index],
        norm.tolist(),
    )


def _monthly_returns(equity: pd.Series) -> dict[str, float]:
    monthly = equity.resample("ME").last().dropna()
    ret = monthly.pct_change().dropna()
    return {ts.strftime("%Y-%m"): round(float(r * 100), 2) for ts, r in ret.items()}


def _extract_trades(target_by_date: dict, threshold: float = TRADE_THRESHOLD) -> list[dict]:
    """把月度目標權重序列轉成「有意義的換倉事件」清單。"""
    trades = []
    prev_w: float | None = None
    for ts in sorted(target_by_date.keys()):
        w = float(target_by_date[ts])
        if np.isnan(w):
            continue
        delta = w - (prev_w if prev_w is not None else 0.0)
        if prev_w is None or abs(delta) >= threshold:
            if prev_w is None:
                action = "初始建倉"
            elif delta > 0:
                action = "加碼"
            else:
                action = "減碼"
            trades.append({
                "date": ts.strftime("%Y-%m-%d"),
                "target_weight": round(w, 3),
                "prev_weight": round(prev_w, 3) if prev_w is not None else 0.0,
                "change": round(float(delta), 3),
                "action": action,
            })
            prev_w = w
    return trades


# ── 主回測邏輯 ───────────────────────────────────────────────────────────────

def run(
    market: str,
    price_csv: str,
    factors_csv: str,
    lag_months: int = 1,
    output_json: str | None = None,
) -> tuple[dict, dict]:
    cfg = load_config()
    price_df = pd.read_csv(price_csv)
    factors_df = pd.read_csv(factors_csv).set_index("date")

    target = market_target_weight(market, factors_df, cfg, lag_months=lag_months)

    feed, feed_df = _price_feed(price_df)
    daily_target = target.reindex(feed_df.index, method="ffill")
    target_by_date = {ts: v for ts, v in daily_target.items()}

    strat_equity = _run_strategy(feed, target_by_date)

    close = feed_df["close"]
    bh_equity = INIT_CASH * close / close.iloc[0]
    common = strat_equity.index.intersection(bh_equity.index)
    strat_equity = strat_equity.loc[common]
    bh_equity = bh_equity.loc[common]

    sk = _kpis(strat_equity)
    bk = _kpis(bh_equity)

    print(f"\n=== {market} 市場溫度分數策略 vs 買進持有 ===")
    print(f"期間 {strat_equity.index[0].date()} ~ {strat_equity.index[-1].date()}  ({sk['years']:.1f} 年)")
    print(f"{'指標':<10}{'分數策略':>16}{'買進持有':>16}")
    print(f"{'CAGR':<10}{sk['cagr']*100:>15.2f}%{bk['cagr']*100:>15.2f}%")
    print(f"{'Sharpe':<10}{sk['sharpe']:>16.2f}{bk['sharpe']:>16.2f}")
    print(f"{'MaxDD':<10}{sk['max_dd']*100:>15.2f}%{bk['max_dd']*100:>15.2f}%")
    print(f"{'期末資產':<10}{sk['final']:>16,.0f}{bk['final']:>16,.0f}")
    print("\n註：分數策略以現金降低曝險，預期 CAGR 多半略低於買進持有，"
          "但若 MaxDD 明顯較小、Sharpe 較高，代表這套燈號對控制風險有效。")

    if output_json:
        s_dates, s_vals = _monthly_equity(strat_equity)
        b_dates, b_vals = _monthly_equity(bh_equity)
        trades = _extract_trades(target_by_date)
        result = {
            "market": market,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "period": {
                "start": strat_equity.index[0].strftime("%Y-%m-%d"),
                "end": strat_equity.index[-1].strftime("%Y-%m-%d"),
                "years": sk["years"],
            },
            "strategy_kpis": sk,
            "buyhold_kpis": bk,
            "equity_monthly": {
                "dates": s_dates,
                "strategy": s_vals,
                "buyhold": b_vals,
            },
            "monthly_returns": {
                "strategy": _monthly_returns(strat_equity),
                "buyhold": _monthly_returns(bh_equity),
            },
            "trades": trades,
        }
        out = Path(output_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n→ JSON 結果已寫入 {out}")

    return sk, bk


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", required=True, choices=["US", "TW"])
    ap.add_argument("--price", required=True)
    ap.add_argument("--factors", required=True)
    ap.add_argument("--lag-months", type=int, default=1)
    ap.add_argument("--output-json", default=None,
                    help="若指定，將詳細結果（KPI/權益曲線/交易記錄）寫入此 JSON 路徑")
    args = ap.parse_args()
    run(args.market, args.price, args.factors, args.lag_months, args.output_json)


if __name__ == "__main__":
    main()
