"""成長股選股 Agent 回測 CLI。

驗證 growth_agent.GrowthAgent 的選股邏輯是否真的有「選股價值」：

  Top-N 動態選股（每月依因子排名換股）
    vs 候選池等權重買進持有（不選股，看選股本身的 alpha）
    vs 大盤 benchmark（0050 / SPY）

用法：
    python run_growth_backtest.py --market TW --start 2025-12-01 --top-n 5
    python run_growth_backtest.py --market US --start 2025-12-01 --top-n 5 --skip-fetch

資料來源
--------
  TW: FinMind TaiwanStockPrice + TaiwanStockMonthRevenue（可設 FINMIND_TOKEN 提高限額）
  US: Yahoo Finance chart API
  --skip-fetch：略過下載，直接用 backtest/data/ 本地快取（demo 用）
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).with_name("data")
RESULTS_DIR = Path(__file__).with_name("results")

from growth_agent import CANDIDATES, CANDIDATE_NAMES, GrowthAgent
from portfolio import _kpis, INIT_CASH, TX_COST_ONEWAY

BENCHMARK = {"TW": "0050", "US": "SPY"}


# ── 資料抓取（含本地快取）───────────────────────────────────────────────────────

def _yahoo_price(symbol: str, start: str) -> pd.Series | None:
    import json as _json
    import ssl
    import time
    import urllib.request

    ctx = ssl.create_default_context()
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"),
    }
    period1 = int(pd.Timestamp(start).timestamp())
    period2 = int(pd.Timestamp("2099-01-01").timestamp())
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?period1={period1}&period2={period2}&interval=1d")
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers)
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


def _finmind_price(symbol: str, start: str) -> pd.Series | None:
    import json as _json
    import os
    import ssl
    import time
    import urllib.parse
    import urllib.request

    ctx = ssl.create_default_context()
    token = os.getenv("FINMIND_TOKEN")
    headers = {"Authorization": "Bearer " + token} if token else {}
    params = {"dataset": "TaiwanStockPrice", "data_id": symbol, "start_date": start}
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


def _finmind_revenue(symbol: str, start: str) -> pd.Series | None:
    """月營收（億元）。index= 該筆營收所屬月份的月底日期。"""
    import json as _json
    import os
    import ssl
    import time
    import urllib.parse
    import urllib.request

    ctx = ssl.create_default_context()
    token = os.getenv("FINMIND_TOKEN")
    headers = {"Authorization": "Bearer " + token} if token else {}
    params = {"dataset": "TaiwanStockMonthRevenue", "data_id": symbol, "start_date": start}
    url = "https://api.finmindtrade.com/api/v4/data?" + urllib.parse.urlencode(params)
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
                obj = _json.loads(r.read())
            if obj.get("status") != 200:
                raise ValueError(obj.get("msg"))
            df = pd.DataFrame(obj.get("data", []))
            if df.empty:
                raise ValueError("無資料")
            idx = (pd.to_datetime(dict(year=df["revenue_year"], month=df["revenue_month"], day=1))
                   + pd.offsets.MonthEnd(0))
            s = pd.to_numeric(df["revenue"], errors="coerce")
            s.index = idx
            s = s.sort_index()
            s.name = symbol
            print(f"  ✅ {symbol} 月營收: {len(s)} 筆")
            return s
        except Exception as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
            else:
                print(f"  ⚠ {symbol} 月營收: {exc}")
                return None


def _load_or_fetch_price(symbol: str, start: str, market: str, skip_fetch: bool) -> pd.Series | None:
    cached = DATA_DIR / f"price_{symbol.replace('/', '_')}.csv"
    if cached.exists():
        s = pd.read_csv(cached, index_col=0, parse_dates=True)["close"]
        s = s[s.index >= pd.Timestamp(start)]
        if len(s) > 0:
            s.name = symbol
            print(f"  ♻ {symbol}: 讀取快取 {cached.name} ({len(s)} 日)")
            return s
    if skip_fetch:
        print(f"  ⚠ {symbol}: 無快取，略過（--skip-fetch）")
        return None
    s = _finmind_price(symbol, start) if market == "TW" else _yahoo_price(symbol, start)
    if s is not None and len(s) > 0:
        DATA_DIR.mkdir(exist_ok=True)
        s.rename("close").to_frame().to_csv(cached, index_label="date")
    return s


def _load_or_fetch_revenue(symbol: str, start: str, skip_fetch: bool) -> pd.Series | None:
    cached = DATA_DIR / f"revenue_{symbol}.csv"
    if cached.exists():
        s = pd.read_csv(cached, index_col=0, parse_dates=True)["revenue"]
        s = s[s.index >= pd.Timestamp(start)]
        if len(s) > 0:
            s.name = symbol
            print(f"  ♻ {symbol} 月營收: 讀取快取 {cached.name} ({len(s)} 筆)")
            return s
    if skip_fetch:
        print(f"  ⚠ {symbol} 月營收: 無快取，略過（--skip-fetch）")
        return None
    s = _finmind_revenue(symbol, start)
    if s is not None and len(s) > 0:
        DATA_DIR.mkdir(exist_ok=True)
        s.rename("revenue").to_frame().to_csv(cached, index_label="date")
    return s


# ── 回測引擎 ─────────────────────────────────────────────────────────────────

def _equity_from_returns(returns: pd.Series, start_ts: pd.Timestamp, init_cash: float) -> pd.Series:
    cum = (1 + returns.fillna(0)).cumprod()
    cum = cum[cum.index >= start_ts]
    if cum.empty:
        return pd.Series(dtype=float)
    cum = cum / cum.iloc[0]
    return cum * init_cash


def backtest(
    factor_df: pd.DataFrame,
    price_df: pd.DataFrame,
    candidates: list[str],
    top_n: int,
    start: str,
    init_cash: float = INIT_CASH,
) -> dict:
    """回傳 top_n / equal_weight 兩腿的 equity 曲線、KPI、每月持股明細。"""
    monthly_price = price_df.resample("ME").last()
    price_ret = monthly_price.pct_change()

    common = factor_df.index.intersection(monthly_price.index)
    if len(common) < 2:
        raise ValueError(f"因子與價格重疊資料不足（{len(common)} 月）")

    top_n_rets, ew_rets, monthly_detail = {}, {}, []
    prev_picks: list[str] = []

    for i in range(1, len(common)):
        decide_date, ret_date = common[i - 1], common[i]

        ranked = factor_df.loc[decide_date].dropna().sort_values(ascending=False)
        picks = [s for s in ranked.head(top_n).index if s in price_ret.columns]

        pick_rets = price_ret.loc[ret_date, picks].dropna() if picks else pd.Series(dtype=float)
        turnover = len(set(picks) ^ set(prev_picks)) / max(len(picks), len(prev_picks), 1)
        tx_cost = turnover * TX_COST_ONEWAY * 2
        top_ret = (pick_rets.mean() - tx_cost) if len(pick_rets) > 0 else 0.0

        ew_cols = [c for c in candidates if c in price_ret.columns]
        ew_pick_rets = price_ret.loc[ret_date, ew_cols].dropna() if ew_cols else pd.Series(dtype=float)
        ew_ret = ew_pick_rets.mean() if len(ew_pick_rets) > 0 else 0.0

        top_n_rets[ret_date] = top_ret
        ew_rets[ret_date] = ew_ret

        # 完整排名（含每檔當月實現報酬），供前端互動調整 Top-N 即時重算
        ranking = [
            {
                "symbol": s,
                "name": CANDIDATE_NAMES.get(s, s),
                "factor": round(float(v), 2),
                "ret_pct": (round(float(price_ret.loc[ret_date, s]) * 100, 2)
                            if s in price_ret.columns and not pd.isna(price_ret.loc[ret_date, s])
                            else None),
            }
            for s, v in ranked.items()
        ]

        monthly_detail.append({
            "date": ret_date.strftime("%Y-%m-%d"),
            "picks": [
                {"symbol": s, "name": CANDIDATE_NAMES.get(s, s), "factor": round(float(ranked[s]), 2)}
                for s in picks
            ],
            "ranking": ranking,
            "top_n_ret_pct": round(top_ret * 100, 2),
            "equal_weight_ret_pct": round(ew_ret * 100, 2),
        })

        prev_picks = picks

    start_ts = pd.Timestamp(start)
    top_n_eq = _equity_from_returns(pd.Series(top_n_rets), start_ts, init_cash)
    ew_eq = _equity_from_returns(pd.Series(ew_rets), start_ts, init_cash)
    monthly_detail = [d for d in monthly_detail if pd.Timestamp(d["date"]) >= start_ts]

    return {
        "top_n_equity": top_n_eq,
        "top_n_kpis": _kpis(top_n_eq),
        "equal_weight_equity": ew_eq,
        "equal_weight_kpis": _kpis(ew_eq),
        "monthly_detail": monthly_detail,
    }


# ── 輸出格式化 ────────────────────────────────────────────────────────────────

def _equity_to_export(eq: pd.Series) -> dict:
    if len(eq) == 0:
        return {}
    norm = (eq / eq.iloc[0] * 100).round(2)
    return {
        "dates": [ts.strftime("%Y-%m-%d") for ts in norm.index],
        "values": norm.tolist(),
    }


def _print_table(market: str, top_n: int, detail: list[dict]) -> None:
    print(f"\n{'─'*70}")
    print(f"  {market} 成長股 Agent  Top-{top_n}  每月持股與報酬")
    print(f"{'─'*70}")
    for d in detail:
        names = ", ".join(f"{p['symbol']}({p['factor']:+.1f})" for p in d["picks"])
        print(f"  {d['date'][:7]:<8} Top-N {d['top_n_ret_pct']:>+6.2f}%"
              f"  全候選等權 {d['equal_weight_ret_pct']:>+6.2f}%   持股: {names}")


def _print_kpis(market: str, top_n: int, result: dict, bh_kpis: dict, bh_sym: str) -> None:
    print(f"\n  {'':16} {'CAGR':>8} {'Sharpe':>8} {'MaxDD':>8}")
    rows = [
        (f"Top-{top_n} 選股", result["top_n_kpis"]),
        ("候選池等權重", result["equal_weight_kpis"]),
        (f"買進持有 {bh_sym}", bh_kpis),
    ]
    for label, k in rows:
        if not k:
            continue
        print(f"  {label:<16} {k.get('cagr',0)*100:>7.2f}%"
              f" {k.get('sharpe',0):>8.3f} {k.get('max_dd',0)*100:>7.2f}%")


# ── 主流程 ───────────────────────────────────────────────────────────────────

def run(market: str, start: str, top_n: int, lag_months: int,
        skip_fetch: bool, output_json: str | None) -> None:
    agent = GrowthAgent(market=market, top_n=top_n, lag_months=lag_months)
    candidates = agent.candidates
    bh_sym = BENCHMARK[market]

    fetch_start = (pd.Timestamp(start) - pd.DateOffset(months=15)).strftime("%Y-%m-%d")
    revenue_fetch_start = (pd.Timestamp(start) - pd.DateOffset(months=27)).strftime("%Y-%m-%d")

    print(f"\n{'='*70}")
    print(f"  成長股選股 Agent 回測  {market}  Top-{top_n}  ({start} 起)")
    print(f"{'='*70}")

    print("\n[1/3] 抓取候選股價格…")
    prices: dict[str, pd.Series] = {}
    for sym in candidates:
        s = _load_or_fetch_price(sym, fetch_start, market, skip_fetch)
        if s is not None:
            prices[sym] = s
    bh_price = _load_or_fetch_price(bh_sym, fetch_start, market, skip_fetch)
    if bh_price is not None:
        prices[bh_sym] = bh_price

    if not prices:
        print("❌ 無可用價格資料，中止。")
        sys.exit(1)

    print("\n[2/3] 計算選股因子…")
    if market == "TW":
        revenue: dict[str, pd.Series] = {}
        for sym in candidates:
            s = _load_or_fetch_revenue(sym, revenue_fetch_start, skip_fetch)
            if s is not None:
                revenue[sym] = s
        if not revenue:
            print("❌ 無可用月營收資料，中止。")
            sys.exit(1)
        factor_df = agent.factor(revenue=revenue)
    else:
        factor_df = agent.factor(prices={s: prices[s] for s in candidates if s in prices})

    factor_df = factor_df.dropna(how="all")
    if factor_df.empty:
        print("❌ 因子計算結果為空（資料長度可能不足 12 個月），中止。")
        sys.exit(1)

    print(f"  因子涵蓋：{factor_df.index[0].strftime('%Y-%m')} ~ {factor_df.index[-1].strftime('%Y-%m')}"
          f"  ({len(factor_df)} 月)")

    print("\n[3/3] 回測 Top-N vs 等權重 vs 大盤…")
    price_df = pd.DataFrame({sym: s for sym, s in prices.items() if sym in candidates})
    result = backtest(factor_df, price_df, candidates, top_n, start)

    bh_eq = pd.Series(dtype=float)
    bh_kpis: dict = {}
    if bh_sym in prices:
        bh_monthly = prices[bh_sym].resample("ME").last()
        bh_monthly = bh_monthly[bh_monthly.index >= pd.Timestamp(start)]
        if len(bh_monthly) > 1:
            bh_eq = INIT_CASH * bh_monthly / bh_monthly.iloc[0]
            bh_kpis = _kpis(bh_eq)

    _print_table(market, top_n, result["monthly_detail"])
    _print_kpis(market, top_n, result, bh_kpis, bh_sym)

    if output_json:
        agent_picks = agent.monthly_picks(factor_df)
        payload = {
            "type": "growth",
            "market": market,
            "start": start,
            "top_n": top_n,
            "lag_months": lag_months,
            "init_cash": INIT_CASH,
            "tx_cost_oneway": TX_COST_ONEWAY,
            "candidates": [{"symbol": s, "name": CANDIDATE_NAMES.get(s, s)} for s in candidates],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "monthly_picks": {
                ts.strftime("%Y-%m-%d"): picks for ts, picks in agent_picks.items()
            },
            "top_n_kpis": result["top_n_kpis"],
            "top_n_equity_monthly": _equity_to_export(result["top_n_equity"]),
            "equal_weight_kpis": result["equal_weight_kpis"],
            "equal_weight_equity_monthly": _equity_to_export(result["equal_weight_equity"]),
            "benchmark": bh_sym,
            "benchmark_kpis": bh_kpis,
            "benchmark_equity_monthly": _equity_to_export(bh_eq),
            "monthly_detail": result["monthly_detail"],
        }
        RESULTS_DIR.mkdir(exist_ok=True)
        out = Path(output_json)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n→ JSON 結果已寫入 {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description="成長股選股 Agent 回測")
    ap.add_argument("--market", choices=["US", "TW"], default="TW")
    ap.add_argument("--start", default="2010-01-01", help="回測起始日 YYYY-MM-DD")
    ap.add_argument("--top-n", type=int, default=5)
    ap.add_argument("--lag-months", type=int, default=1, help="TW 月營收公布落差（月）")
    ap.add_argument("--skip-fetch", action="store_true", help="略過資料下載，直接用本地快取")
    ap.add_argument("--output-json", default=None, help="輸出 JSON 路徑（選用）")
    args = ap.parse_args()

    run(
        market=args.market,
        start=args.start,
        top_n=args.top_n,
        lag_months=args.lag_months,
        skip_fetch=args.skip_fetch,
        output_json=args.output_json,
    )


if __name__ == "__main__":
    main()
