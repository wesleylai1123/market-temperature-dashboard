"""回測「市場溫度分數」多策略 vs 買進持有。

每個策略共用相同的合成分數序列，只有分數→權重的映射函式不同。
一次跑完所有（或指定的）策略並輸出比較報告。

用法：
    # 執行所有已登錄策略並列印比較
    python run_backtest.py --market US --price data/us_price.csv --factors data/us_factors.csv

    # 只跑指定策略
    python run_backtest.py --market US --price data/us_price.csv --factors data/us_factors.csv \\
        --strategies linear,band

    # 輸出 JSON（供儀表板顯示）
    python run_backtest.py --market US --price data/us_price.csv --factors data/us_factors.csv \\
        --output-json results/us_results.json

    # 列出所有可用策略
    python run_backtest.py --list-strategies
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import backtrader as bt
import numpy as np
import pandas as pd

from scores import load_config, market_composite_score
from strategies import REGISTRY, list_strategies

INIT_CASH = 1_000_000.0
ANN = 252
TRADE_THRESHOLD = 0.05  # 目標權重變化超過此值才記為一次換倉


# ── Backtrader 元件 ───────────────────────────────────────────────────────────

class EquityRecorder(bt.Analyzer):
    def start(self):
        self.dates, self.values = [], []

    def next(self):
        self.dates.append(self.strategy.datetime.date(0))
        self.values.append(self.strategy.broker.getvalue())

    def get_analysis(self) -> pd.Series:
        return pd.Series(self.values, index=pd.to_datetime(self.dates))


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
    """月底採樣並標準化至起始值 100。"""
    monthly = equity.resample("ME").last().dropna()
    if monthly.empty:
        return [], []
    norm = (monthly / monthly.iloc[0] * 100.0).round(2)
    return ([ts.strftime("%Y-%m-%d") for ts in norm.index], norm.tolist())


def _monthly_returns(equity: pd.Series) -> dict[str, float]:
    monthly = equity.resample("ME").last().dropna()
    ret = monthly.pct_change().dropna()
    return {ts.strftime("%Y-%m"): round(float(r * 100), 2) for ts, r in ret.items()}


def _extract_trades(target_by_date: dict) -> list[dict]:
    trades, prev_w = [], None
    for ts in sorted(target_by_date.keys()):
        w = float(target_by_date[ts])
        if np.isnan(w):
            continue
        delta = w - (prev_w if prev_w is not None else 0.0)
        if prev_w is None or abs(delta) >= TRADE_THRESHOLD:
            action = "初始建倉" if prev_w is None else ("加碼" if delta > 0 else "減碼")
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
    strategy_names: list[str] | None = None,
    lag_months: int = 1,
    output_json: str | None = None,
) -> dict:
    """回傳 {strategy_name: kpis} 的比較字典，含 "buyhold" 鍵。"""
    cfg = load_config()
    price_df = pd.read_csv(price_csv)
    factors_df = pd.read_csv(factors_csv).set_index("date")

    # 所有策略共用同一條合成分數序列
    scores = market_composite_score(market, factors_df, cfg, lag_months)

    feed, feed_df = _price_feed(price_df)
    close = feed_df["close"]
    bh_equity = INIT_CASH * close / close.iloc[0]

    names = strategy_names or list(REGISTRY.keys())
    # 過濾掉不存在的（給友善錯誤訊息）
    for n in names:
        if n not in REGISTRY:
            raise ValueError(f"未知策略 '{n}'，可用：{sorted(REGISTRY)}")

    results: dict[str, dict] = {}

    for name in names:
        strat_obj = REGISTRY[name]
        target = strat_obj.apply_series(scores).clip(0, 1)
        daily_target = target.reindex(feed_df.index, method="ffill")
        target_by_date = {ts: v for ts, v in daily_target.items()}

        equity = _run_strategy(feed, target_by_date)
        common = equity.index.intersection(bh_equity.index)
        equity_aligned = equity.loc[common]
        bh_aligned = bh_equity.loc[common]

        kpis = _kpis(equity_aligned)
        results[name] = {
            "description": strat_obj.description,
            "kpis": kpis,
            "_equity": equity_aligned,
            "_bh_equity": bh_aligned,
            "_target_by_date": target_by_date,
        }

    # ── 印出比較表 ───────────────────────────────────────────────────────────
    # 取任一已跑的 bh_equity 做對照（都相同）
    first = next(iter(results.values()))
    bh_eq = first["_bh_equity"]
    bk = _kpis(bh_eq)

    period_start = bh_eq.index[0].date()
    period_end = bh_eq.index[-1].date()

    print(f"\n=== {market} 市場溫度分數 — 策略比較 vs 買進持有 ===")
    print(f"期間 {period_start} ~ {period_end}  ({bk['years']:.1f} 年)\n")
    header = f"{'策略':<18} {'CAGR':>8} {'Sharpe':>8} {'MaxDD':>8} {'期末(萬)':>10}"
    print(header)
    print("-" * len(header))
    print(f"{'買進持有':<18} {bk['cagr']*100:>7.2f}%"
          f" {bk['sharpe']:>8.2f} {bk['max_dd']*100:>7.2f}%"
          f" {bk['final']/1e4:>9.0f}")
    for name, r in results.items():
        k = r["kpis"]
        print(f"  {name:<16} {k['cagr']*100:>7.2f}%"
              f" {k['sharpe']:>8.2f} {k['max_dd']*100:>7.2f}%"
              f" {k['final']/1e4:>9.0f}")
    print("\n提示：分數策略以現金控制曝險；若 MaxDD 顯著較小且 Sharpe 較高，代表溫度訊號對風險控制有效。")

    # ── 輸出 JSON ────────────────────────────────────────────────────────────
    if output_json:
        bh_dates, bh_vals = _monthly_equity(bh_eq)
        export_strategies = {}
        for name, r in results.items():
            eq = r["_equity"]
            s_dates, s_vals = _monthly_equity(eq)
            export_strategies[name] = {
                "description": r["description"],
                "kpis": r["kpis"],
                "equity_monthly": {"dates": s_dates, "values": s_vals},
                "monthly_returns": _monthly_returns(eq),
                "trades": _extract_trades(r["_target_by_date"]),
            }

        payload = {
            "market": market,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "period": {
                "start": str(period_start),
                "end": str(period_end),
                "years": bk["years"],
            },
            "buyhold_kpis": bk,
            "buyhold_equity_monthly": {"dates": bh_dates, "values": bh_vals},
            "strategies": export_strategies,
        }
        out = Path(output_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n→ JSON 結果已寫入 {out}")

    return {n: r["kpis"] for n, r in results.items()} | {"buyhold": bk}


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="市場溫度分數回測工具")
    ap.add_argument("--list-strategies", action="store_true", help="列出所有可用策略後退出")
    ap.add_argument("--market", choices=["US", "TW"])
    ap.add_argument("--price", help="價格 CSV（date,close）")
    ap.add_argument("--factors", help="因子 CSV（date,valuation,sentiment,...）")
    ap.add_argument(
        "--strategies",
        default=None,
        help="逗號分隔的策略名稱（預設：全部）。例：--strategies linear,band,sigmoid",
    )
    ap.add_argument("--lag-months", type=int, default=1)
    ap.add_argument("--output-json", default=None, help="輸出 JSON 路徑")
    args = ap.parse_args()

    if args.list_strategies:
        list_strategies()
        return

    if not all([args.market, args.price, args.factors]):
        ap.error("需指定 --market、--price、--factors（或使用 --list-strategies）")

    names = [s.strip() for s in args.strategies.split(",")] if args.strategies else None
    run(args.market, args.price, args.factors, names, args.lag_months, args.output_json)


if __name__ == "__main__":
    main()
