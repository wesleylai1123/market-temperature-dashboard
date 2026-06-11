"""整合式投資組合回測 CLI。

整合三層：
  溫度分數  ×  Universe（選什麼）  ×  Strategy（買多少）

用法：
    # 跑全部 Universe × 指定策略，預設起始 2010
    python run_portfolio.py --market US --strategy band

    # 只跑近半年，指定 Universe
    python run_portfolio.py --market US --start 2025-12-01 \\
        --universe us_sector --strategy band

    # 台股
    python run_portfolio.py --market TW --start 2025-12-01 \\
        --universe tw_etf --strategy conservative

    # 列出所有可用 Universe / Strategy
    python run_portfolio.py --list

資料來源
--------
  * 自動呼叫 fetch_history.py 取得所需標的的價格 + 因子
  * 需要網路；台股需要 FinMind API（可設 FINMIND_TOKEN）
  * 已有本地 CSV 時可用 --skip-fetch 略過下載
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(__file__).with_name("data")
RESULTS_DIR = Path(__file__).with_name("results")

# ── 延遲 import（不安裝 backtrader 也能用本檔）
from scores import load_config, market_composite_score
from strategies import REGISTRY as STRAT_REG, get as get_strategy, list_strategies
from universe import REGISTRY as UNI_REG, get as get_universe, list_universes
import portfolio as pf


# ── 各標的的 Yahoo 代碼對照 ─────────────────────────────────────────────────

YAHOO_MAP = {
    "SPY":  "SPY",
    "QQQ":  "QQQ",
    "XLK":  "XLK",
    "XLU":  "XLU",
    "XLV":  "XLV",
    "MTUM": "MTUM",
    "QUAL": "QUAL",
    "USMV": "USMV",
    "SPLV": "SPLV",
}

FINMIND_STOCKS = {
    "0050": "0050",
    "0056": "0056",
}


def _yahoo_price(symbol: str, start: str) -> pd.Series | None:
    """Yahoo Finance 日收盤（已還原權值）。"""
    import json as _json
    import ssl
    import time
    import urllib.request

    ctx = ssl.create_default_context()
    browser = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"),
    }
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?period1={_ts(start)}&period2={_ts('2099-01-01')}&interval=1d")
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=browser)
            with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
                res = _json.loads(r.read())
            result = res["chart"]["result"][0]
            idx = pd.to_datetime(result["timestamp"], unit="s").normalize()
            ind = result["indicators"]
            closes = (ind.get("adjclose", [{}])[0].get("adjclose")
                      or ind["quote"][0]["close"])
            s = pd.Series(closes, index=idx, name=symbol).dropna()
            s = s[s.index >= pd.Timestamp(start)]
            print(f"  ✅ {symbol}: {len(s)} 日")
            return s
        except Exception as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
            else:
                print(f"  ⚠ {symbol}: {exc}")
                return None


def _ts(date_str: str) -> int:
    return int(pd.Timestamp(date_str).timestamp())


def _finmind_price(symbol: str, start: str) -> pd.Series | None:
    import json as _json
    import os
    import ssl
    import time
    import urllib.parse
    import urllib.request

    ctx = ssl.create_default_context()
    token = os.getenv("FINMIND_TOKEN")
    params = {"dataset": "TaiwanStockPrice", "data_id": symbol, "start_date": start}
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    url = "https://api.finmindtrade.com/api/v4/data?" + urllib.parse.urlencode(params)
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
                obj = _json.loads(r.read())
            if obj.get("status") != 200:
                raise ValueError(obj.get("msg"))
            df = pd.DataFrame(obj.get("data", []))
            df["date"] = pd.to_datetime(df["date"])
            s = pd.to_numeric(df.set_index("date")["close"], errors="coerce").dropna()
            s.name = symbol
            print(f"  ✅ {symbol}: {len(s)} 日")
            return s
        except Exception as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
            else:
                print(f"  ⚠ {symbol}: {exc}")
                return None


def fetch_prices(symbols: list[str], start: str, market: str) -> dict[str, pd.Series]:
    prices = {}
    for sym in symbols:
        cached = DATA_DIR / f"price_{sym.replace('/', '_')}.csv"
        if cached.exists():
            s = pd.read_csv(cached, index_col=0, parse_dates=True)["close"]
            s = s[s.index >= pd.Timestamp(start)]
            if len(s) > 0:
                s.name = sym
                prices[sym] = s
                print(f"  ♻ {sym}: 讀取快取 {cached.name} ({len(s)} 日)")
                continue
        if sym in YAHOO_MAP:
            s = _yahoo_price(YAHOO_MAP[sym], start)
        elif sym in FINMIND_STOCKS or market == "TW":
            s = _finmind_price(sym, start)
        else:
            s = _yahoo_price(sym, start)
        if s is not None and len(s) > 0:
            prices[sym] = s
            # 快取到 CSV
            DATA_DIR.mkdir(exist_ok=True)
            s.rename("close").to_frame().to_csv(cached, index_label="date")
    return prices


def fetch_factors(market: str, start: str) -> pd.DataFrame | None:
    """嘗試從現有 CSV 讀取因子；若不存在則嘗試從 fetch_history 抓。"""
    csv_path = DATA_DIR / f"{market.lower()}_factors.csv"
    if csv_path.exists():
        df = pd.read_csv(csv_path, index_col="date", parse_dates=True)
        df = df[df.index >= pd.Timestamp(start)]
        if len(df) > 0:
            print(f"  ♻ {market} 因子：讀取快取 {csv_path.name} ({len(df)} 列)")
            return df

    print(f"  ⬇ {market} 因子：嘗試即時抓取…")
    try:
        import fetch_history as fh
        start_year = int(start[:4])
        start_ts = pd.Timestamp(start)
        DATA_DIR.mkdir(exist_ok=True)

        if market == "US":
            cols = []
            cape = fh._try("CAPE", fh.us_cape)
            if cape is not None:
                cols.append(cape)
            fg = fh._try("Fear&Greed", fh.us_fear_greed)
            if fg is not None:
                cols.append(fg)
            tre = fh._try("Treasury", lambda: fh.us_treasury(start_year))
            if tre is not None:
                cols += [tre["cycle"], tre["rate"]]
        else:
            cols = []
            per = fh._try("2330 PER", lambda: fh.tw_valuation(start))
            if per is not None:
                cols.append(per)
            margin = fh._try("融資", lambda: fh.tw_sentiment(start))
            if margin is not None:
                cols.append(margin)
            foreign = fh._try("外資", lambda: fh.tw_foreign(start))
            if foreign is not None:
                cols.append(foreign)

        if not cols:
            return None
        df = pd.concat(cols, axis=1).sort_index()
        df = df[df.index >= start_ts]
        df.index.name = "date"
        df.to_csv(csv_path)
        return df
    except Exception as exc:
        print(f"  ⚠ 因子抓取失敗: {exc}")
        return None


# ── 輸出格式化 ────────────────────────────────────────────────────────────────

def _equity_to_export(eq: pd.Series) -> dict:
    norm = (eq / eq.iloc[0] * 100).round(2)
    return {
        "dates":  [ts.strftime("%Y-%m-%d") for ts in norm.index],
        "values": norm.tolist(),
    }


def _print_table(market: str, uni_name: str, strat_name: str,
                 detail: list[dict], bh_monthly: pd.Series) -> None:
    print(f"\n{'─'*62}")
    print(f"  {market} | Universe: {uni_name} | Strategy: {strat_name}")
    print(f"{'─'*62}")
    print(f"  {'月份':<10} {'分數':>5} {'模式':>4} {'持倉':<22} {'股%':>4} {'月報酬%':>8}")
    print(f"  {'─'*58}")
    for d in detail:
        print(f"  {d['date'][:7]:<10} {d['score']:>5.1f}"
              f" {d['mode']:>4} {','.join(d['assets']):<22}"
              f" {d['equity_weight']:>4.0f} {d['port_ret_pct']:>8.2f}%")

    # 買進持有對照
    bh_ret = bh_monthly.pct_change().dropna()
    print(f"\n  {'買進持有月報酬：'}", end="")
    for ts, r in bh_ret.items():
        print(f"  {ts.strftime('%Y-%m')} {r*100:+.2f}%", end="")
    print()


# ── 主流程 ───────────────────────────────────────────────────────────────────

def run(
    market: str,
    start: str,
    universe_names: list[str],
    strategy_names: list[str],
    skip_fetch: bool = False,
    output_json: str | None = None,
) -> None:
    cfg = load_config()

    # 決定需要哪些標的
    all_symbols: set[str] = set()
    universes_objs = [get_universe(n) for n in universe_names]
    for u in universes_objs:
        all_symbols.update(u.all_symbols())
    all_symbols = list(all_symbols)

    print(f"\n{'='*62}")
    print(f"  市場溫度投資組合回測  {market}  ({start} 起)")
    print(f"  Universe: {universe_names}  Strategy: {strategy_names}")
    print(f"{'='*62}")

    # 抓價格
    if skip_fetch:
        prices = {}
        for sym in all_symbols:
            cached = DATA_DIR / f"price_{sym.replace('/', '_')}.csv"
            if cached.exists():
                s = pd.read_csv(cached, index_col=0, parse_dates=True)["close"]
                s = s[s.index >= pd.Timestamp(start)]
                s.name = sym
                prices[sym] = s
    else:
        print("\n[1/3] 抓取標的價格…")
        prices = fetch_prices(all_symbols, start, market)

    # 抓因子
    print("\n[2/3] 取得因子資料…")
    factors_df = fetch_factors(market, start)
    if factors_df is None or len(factors_df) == 0:
        print("❌ 無法取得因子資料，中止。")
        sys.exit(1)

    # 計算合成分數（lag=1）
    print("\n[3/3] 計算合成分數並回測…")
    scores = market_composite_score(market, factors_df, cfg, lag_months=1)
    if len(scores) < 2:
        print(f"❌ 有效分數不足（{len(scores)} 月），中止。")
        sys.exit(1)

    print(f"  分數序列：{scores.index[0].strftime('%Y-%m')} ~ {scores.index[-1].strftime('%Y-%m')}  ({len(scores)} 月)")
    print(f"  分數範圍：min={scores.min():.1f}  mean={scores.mean():.1f}  max={scores.max():.1f}")

    export_universes: dict[str, dict] = {}

    for uni in universes_objs:
        export_strategies: dict[str, dict] = {}
        bh_result = pf.run_benchmark(uni.benchmark, prices)
        bh_eq = bh_result["equity_monthly"]

        for strat_name in strategy_names:
            strat = get_strategy(strat_name)
            try:
                result = pf.run(scores, prices, uni, strat)
            except ValueError as e:
                print(f"  ⚠ {uni.name} × {strat_name}: {e}")
                continue

            eq = result["equity_monthly"]

            _print_table(market, uni.name, strat_name, result["monthly_detail"], bh_eq)
            k = result["kpis"]
            bk = bh_result["kpis"]
            print(f"\n  {'':14} {'CAGR':>8} {'Sharpe':>8} {'MaxDD':>8}")
            print(f"  {'  ' + strat_name:<14} {k.get('cagr',0)*100:>7.2f}%"
                  f" {k.get('sharpe',0):>8.2f} {k.get('max_dd',0)*100:>7.2f}%")
            if bk:
                print(f"  {'  買進持有':<14} {bk.get('cagr',0)*100:>7.2f}%"
                      f" {bk.get('sharpe',0):>8.2f} {bk.get('max_dd',0)*100:>7.2f}%")

            export_strategies[strat_name] = {
                "strategy_description": strat.description,
                "kpis": result["kpis"],
                "equity_monthly": _equity_to_export(eq) if len(eq) > 0 else {},
                "trades": result["trades"],
                "monthly_detail": result["monthly_detail"],
            }

        export_universes[uni.name] = {
            "universe_description": uni.description,
            "benchmark": uni.benchmark,
            "buyhold_kpis": bh_result["kpis"],
            "buyhold_equity_monthly": _equity_to_export(bh_eq) if len(bh_eq) > 0 else {},
            "strategies": export_strategies,
        }

    if output_json:
        payload = {
            "type": "portfolio",
            "market": market,
            "start": start,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "score_summary": {
                "count": len(scores),
                "min":   round(float(scores.min()), 1),
                "mean":  round(float(scores.mean()), 1),
                "max":   round(float(scores.max()), 1),
            },
            "universes": export_universes,
        }
        RESULTS_DIR.mkdir(exist_ok=True)
        out = Path(output_json)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n→ JSON 結果已寫入 {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description="市場溫度整合投資組合回測")
    ap.add_argument("--list", action="store_true", help="列出所有 Universe / Strategy")
    ap.add_argument("--market", choices=["US", "TW"], default="US")
    ap.add_argument("--start",  default="2010-01-01", help="回測起始日 YYYY-MM-DD")
    ap.add_argument("--universe",  default=None,
                    help="Universe 名稱（逗號分隔，預設依市場選所有）")
    ap.add_argument("--symbols", default=None,
                    help="自訂標的清單（逗號分隔），建立 ad-hoc Universe 'custom'，"
                         "曝險比例由 --strategy 決定，標的池固定不隨分數切換。例：--symbols AAPL,NVDA,MSFT")
    ap.add_argument("--benchmark", default=None,
                    help="搭配 --symbols 使用：自訂 Universe 的買進持有對照標的（預設取清單第一檔）")
    ap.add_argument("--strategy",  default="band,conservative",
                    help="Strategy 名稱（逗號分隔）")
    ap.add_argument("--skip-fetch", action="store_true",
                    help="略過資料下載，直接用本地快取")
    ap.add_argument("--output-json", default=None,
                    help="輸出 JSON 路徑（選用）")
    args = ap.parse_args()

    if args.list:
        print("=== Universe ===")
        list_universes()
        print("\n=== Strategy ===")
        list_strategies()
        return

    # 自訂標的清單 → ad-hoc Universe（固定持有，不隨分數切換標的池，僅曝險比例會變）
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        if not symbols:
            ap.error("--symbols 不可為空")
        from universe import Universe, register as register_universe
        register_universe(Universe(
            "custom", args.market, f"自訂標的：{', '.join(symbols)}",
            aggressive=symbols, neutral=symbols, defensive=symbols,
            benchmark=args.benchmark or symbols[0],
        ))
        uni_names = ["custom"]
    elif args.universe:
        uni_names = [u.strip() for u in args.universe.split(",")]
    else:
        uni_names = [n for n, u in UNI_REG.items() if u.market == args.market]

    strat_names = [s.strip() for s in args.strategy.split(",")]

    run(
        market=args.market,
        start=args.start,
        universe_names=uni_names,
        strategy_names=strat_names,
        skip_fetch=args.skip_fetch,
        output_json=args.output_json,
    )


if __name__ == "__main__":
    main()
