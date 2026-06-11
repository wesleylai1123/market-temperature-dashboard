"""回測「市場溫度分數」多策略 vs 買進持有（vectorbt 版，支援多標的）。

每個策略共用相同的合成分數序列，只有分數→權重的映射函式不同。
可同時對多個標的（例如 SPY + AAPL + NVDA）平行跑同一套策略並列比較。

用法：
    # 執行所有已登錄策略並列印比較（單一主要標的）
    python run_backtest.py --market US --price data/us_price.csv --factors data/us_factors.csv

    # 同時比較多個標的（額外標的會自動抓取/快取到 data/price_<SYM>.csv）
    python run_backtest.py --market US --price data/us_price.csv --factors data/us_factors.csv \\
        --symbols AAPL,NVDA --skip-fetch

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

import pandas as pd

import vbt_engine as vbe
from scores import load_config, market_composite_score
from strategies import REGISTRY, list_strategies

INIT_CASH = 1_000_000.0
ANN = 252
TX_COST_ONEWAY = 0.0005  # 0.05% 單邊（與 portfolio.py 一致）
TRADE_THRESHOLD = 0.05   # 目標權重變化超過此值才記為一次換倉

DEFAULT_SYMBOL = {"US": "SPY", "TW": "0050"}


# ── 輔助函式 ─────────────────────────────────────────────────────────────────

def _load_close_csv(price_csv: str) -> pd.Series:
    df = pd.read_csv(price_csv)
    df.index = pd.to_datetime(df["date"])
    close = pd.to_numeric(df["close"], errors="coerce").dropna().sort_index()
    return close


def _extract_trades(target_by_date: dict) -> list[dict]:
    trades, prev_w = [], None
    for ts in sorted(target_by_date.keys()):
        w = float(target_by_date[ts])
        if pd.isna(w):
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


def _run_one_symbol(symbol: str, close: pd.Series, scores: pd.Series, names: list[str]) -> dict:
    """對單一標的跑全部策略 + 買進持有，回傳可序列化的結果 dict（含 _equity 供印表）。"""
    bh_equity = vbe.run_holding(close, INIT_CASH)

    strat_results: dict[str, dict] = {}
    for name in names:
        strat_obj = REGISTRY[name]
        target = strat_obj.apply_series(scores).clip(0, 1)
        daily_target = target.reindex(close.index, method="ffill")

        pf = vbe.run_target_percent(close, daily_target, INIT_CASH, fees=TX_COST_ONEWAY)
        equity = pf.value()

        common = equity.index.intersection(bh_equity.index)
        equity_aligned = equity.loc[common]

        target_by_date = {ts: v for ts, v in daily_target.items()}
        strat_results[name] = {
            "description": strat_obj.description,
            "kpis": vbe.extract_kpis(equity_aligned, ANN),
            "_equity": equity_aligned,
            "_target_by_date": target_by_date,
        }

    bh_common_index = next(iter(strat_results.values()))["_equity"].index if strat_results else bh_equity.index
    bh_aligned = bh_equity.loc[bh_equity.index.intersection(bh_common_index)]

    return {
        "symbol": symbol,
        "buyhold_kpis": vbe.extract_kpis(bh_aligned, ANN),
        "_bh_equity": bh_aligned,
        "strategies": strat_results,
    }


def _print_table(symbol: str, result: dict) -> None:
    bk = result["buyhold_kpis"]
    bh_eq = result["_bh_equity"]
    period_start = bh_eq.index[0].date()
    period_end = bh_eq.index[-1].date()

    print(f"\n=== {symbol} — 策略比較 vs 買進持有 ===")
    print(f"期間 {period_start} ~ {period_end}  ({bk['years']:.1f} 年)\n")
    header = (f"{'策略':<18} {'CAGR':>8} {'Sharpe':>8} {'Sortino':>8}"
              f" {'Calmar':>8} {'MaxDD':>8} {'MaxDD天':>7} {'勝率':>6} {'期末(萬)':>10}")
    print(header)
    print("-" * len(header))

    def _row(label: str, k: dict) -> str:
        sortino = f"{k['sortino']:.2f}" if k.get("sortino") is not None else "  -"
        calmar = f"{k['calmar']:.2f}" if k.get("calmar") is not None else "  -"
        return (f"{label:<18} {k['cagr']*100:>7.2f}% {k['sharpe']:>8.2f}"
                f" {sortino:>8} {calmar:>8} {k['max_dd']*100:>7.2f}%"
                f" {k['max_dd_duration']:>6}d {k['win_rate']*100:>5.1f}%"
                f" {k['final']/1e4:>9.0f}")

    print(_row("買進持有", bk))
    for name, r in result["strategies"].items():
        print(_row(f"  {name}", r["kpis"]))
    print("\n提示：分數策略以現金控制曝險；若 MaxDD 顯著較小且 Sharpe/Sortino 較高，代表溫度訊號對風險控制有效。")


# ── 主回測邏輯 ───────────────────────────────────────────────────────────────

def run(
    market: str,
    price_csv: str,
    factors_csv: str,
    strategy_names: list[str] | None = None,
    lag_months: int = 1,
    extra_symbols: list[str] | None = None,
    skip_fetch: bool = False,
    output_json: str | None = None,
) -> dict:
    """回傳 {symbol: {strategy_name: kpis, "buyhold": kpis}}。"""
    cfg = load_config()
    close = _load_close_csv(price_csv)
    factors_df = pd.read_csv(factors_csv).set_index("date")

    # 所有標的、所有策略共用同一條合成分數序列
    scores = market_composite_score(market, factors_df, cfg, lag_months)

    names = strategy_names or list(REGISTRY.keys())
    for n in names:
        if n not in REGISTRY:
            raise ValueError(f"未知策略 '{n}'，可用：{sorted(REGISTRY)}")

    primary_symbol = DEFAULT_SYMBOL.get(market, market)
    prices: dict[str, pd.Series] = {primary_symbol: close}

    if extra_symbols:
        from run_portfolio import fetch_prices
        start = close.index[0].strftime("%Y-%m-%d")
        if skip_fetch:
            from run_portfolio import DATA_DIR
            for sym in extra_symbols:
                cached = DATA_DIR / f"price_{sym.replace('/', '_')}.csv"
                if cached.exists():
                    s = pd.read_csv(cached, index_col=0, parse_dates=True)["close"]
                    s.name = sym
                    prices[sym] = s
                else:
                    print(f"  ⚠ {sym}: 找不到快取 {cached}，略過")
        else:
            prices.update(fetch_prices(extra_symbols, start, market))

    results: dict[str, dict] = {}
    for symbol, sym_close in prices.items():
        results[symbol] = _run_one_symbol(symbol, sym_close, scores, names)
        _print_table(symbol, results[symbol])

    # ── 輸出 JSON ────────────────────────────────────────────────────────────
    if output_json:
        first = next(iter(results.values()))
        bh_eq = first["_bh_equity"]
        period_start = bh_eq.index[0].date()
        period_end = bh_eq.index[-1].date()
        bk0 = first["buyhold_kpis"]

        export_symbols: dict[str, dict] = {}
        for symbol, r in results.items():
            bh_dates, bh_vals = vbe.monthly_export(r["_bh_equity"])
            bh_dd_dates, bh_dd_vals = vbe.drawdown_monthly_export(r["_bh_equity"])

            export_strategies = {}
            for name, sr in r["strategies"].items():
                eq = sr["_equity"]
                s_dates, s_vals = vbe.monthly_export(eq)
                dd_dates, dd_vals = vbe.drawdown_monthly_export(eq)
                export_strategies[name] = {
                    "description": sr["description"],
                    "kpis": sr["kpis"],
                    "equity_monthly": {"dates": s_dates, "values": s_vals},
                    "drawdown_monthly": {"dates": dd_dates, "values": dd_vals},
                    "monthly_returns": vbe.monthly_returns(eq),
                    "trades": _extract_trades(sr["_target_by_date"]),
                }

            export_symbols[symbol] = {
                "buyhold_kpis": r["buyhold_kpis"],
                "buyhold_equity_monthly": {"dates": bh_dates, "values": bh_vals},
                "buyhold_drawdown_monthly": {"dates": bh_dd_dates, "values": bh_dd_vals},
                "strategies": export_strategies,
            }

        payload = {
            "market": market,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "period": {
                "start": str(period_start),
                "end": str(period_end),
                "years": bk0["years"],
            },
            "symbols": export_symbols,
        }
        out = Path(output_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n→ JSON 結果已寫入 {out}")

    return {
        symbol: {n: r["kpis"] for n, r in res["strategies"].items()} | {"buyhold": res["buyhold_kpis"]}
        for symbol, res in results.items()
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="市場溫度分數回測工具（vectorbt）")
    ap.add_argument("--list-strategies", action="store_true", help="列出所有可用策略後退出")
    ap.add_argument("--market", choices=["US", "TW"])
    ap.add_argument("--price", help="主要標的價格 CSV（date,close）")
    ap.add_argument("--factors", help="因子 CSV（date,valuation,sentiment,...）")
    ap.add_argument(
        "--strategies",
        default=None,
        help="逗號分隔的策略名稱（預設：全部）。例：--strategies linear,band,sigmoid",
    )
    ap.add_argument(
        "--symbols",
        default=None,
        help="額外標的代號（逗號分隔），與主要標的共用同一條分數序列並列比較。例：--symbols AAPL,NVDA",
    )
    ap.add_argument("--skip-fetch", action="store_true",
                     help="額外標的略過下載，直接用 data/price_<SYM>.csv 本地快取")
    ap.add_argument("--lag-months", type=int, default=1)
    ap.add_argument("--output-json", default=None, help="輸出 JSON 路徑")
    args = ap.parse_args()

    if args.list_strategies:
        list_strategies()
        return

    if not all([args.market, args.price, args.factors]):
        ap.error("需指定 --market、--price、--factors（或使用 --list-strategies）")

    names = [s.strip() for s in args.strategies.split(",")] if args.strategies else None
    extra_symbols = [s.strip() for s in args.symbols.split(",")] if args.symbols else None
    run(
        args.market, args.price, args.factors, names, args.lag_months,
        extra_symbols=extra_symbols, skip_fetch=args.skip_fetch, output_json=args.output_json,
    )


if __name__ == "__main__":
    main()
