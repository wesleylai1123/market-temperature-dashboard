"""月度多資產投資組合回測引擎。

核心邏輯
--------
  每月末決策（使用上月底已知分數，避免 lookahead）：
    1. score  ─→ strategy.weight(score)            = 股票總曝險 W
    2. score  ─→ universe.assets_for_score(score)   = 標的池 [A, B, ...]
    3. 各標的等權重 W/N，剩餘 1-W 為現金
    4. 下個月實現報酬 = W * 平均資產報酬 + (1-W) * 現金報酬
    5. 換倉時扣除 TX_COST（雙邊）

與 run_backtest.py（backtrader 版）的差異
-----------------------------------------
  * 本檔處理多資產；run_backtest.py 處理單資產
  * 本檔以月頻計算，不做 tick-level 撮合
  * 適合驗證「選股 + 市場擇時」整合邏輯
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from strategies import Strategy
    from universe import Universe

INIT_CASH = 1_000_000.0
CASH_YIELD_ANNUAL = 0.04   # 假設現金年化 4%（美國貨幣基金水準）
TX_COST_ONEWAY = 0.0005    # 0.05% 單邊（ETF 成本估計）
ANN = 12                   # 月頻年化


def _kpis(equity: pd.Series) -> dict:
    equity = equity.dropna()
    if len(equity) < 2:
        return {}
    ret = equity.pct_change().dropna()
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1 if years > 0 else 0.0
    sharpe = (ret.mean() / ret.std() * np.sqrt(ANN)) if ret.std() > 0 else 0.0
    max_dd = ((equity - equity.cummax()) / equity.cummax()).min()
    return {
        "cagr":   round(float(cagr), 4),
        "sharpe": round(float(sharpe), 3),
        "max_dd": round(float(max_dd), 4),
        "final":  round(float(equity.iloc[-1]), 2),
        "years":  round(years, 2),
    }


def _to_monthly(prices: dict[str, pd.Series]) -> pd.DataFrame:
    """日頻價格 → 月底收盤（resample ME）。"""
    frames = {sym: s.resample("ME").last() for sym, s in prices.items() if s is not None and len(s) > 0}
    if not frames:
        return pd.DataFrame()
    return pd.DataFrame(frames).sort_index()


def run(
    scores: pd.Series,
    prices: dict[str, pd.Series],     # 日頻或月頻收盤價
    universe: "Universe",
    strategy: "Strategy",
    init_cash: float = INIT_CASH,
) -> dict:
    """
    回傳：
      equity_monthly: pd.Series  (月底, 絕對金額)
      kpis:           dict
      trades:         list[dict]
      monthly_detail: list[dict]  (每月明細：分數 / 模式 / 持倉 / 報酬)
    """
    price_df = _to_monthly(prices)
    if price_df.empty:
        raise ValueError("沒有可用的價格資料")

    # 分數對齊月底（scores 應已做 lag）
    score_monthly = scores.copy()
    score_monthly.index = pd.to_datetime(score_monthly.index)

    common = score_monthly.index.intersection(price_df.index)
    if len(common) < 2:
        raise ValueError(f"分數與價格重疊資料不足（{len(common)} 月）")

    score_monthly = score_monthly.reindex(common)
    price_df = price_df.reindex(common)
    price_ret = price_df.pct_change()   # 月報酬率

    cash_monthly_yield = (1 + CASH_YIELD_ANNUAL) ** (1 / 12) - 1

    equity = init_cash
    equity_series = {}
    trades = []
    monthly_detail = []
    prev_assets: list[str] = []

    for i, date in enumerate(common):
        equity_series[date] = equity

        if i == 0:
            continue  # 第一個月只記錄初始值，不交易

        prev_date = common[i - 1]
        score = float(score_monthly.iloc[i - 1])   # 上月分數（已 lag）

        # 選標的 & 計算權重
        target_assets = universe.assets_for_score(score)
        available = [a for a in target_assets
                     if a in price_df.columns and not pd.isna(price_df.loc[date, a])]
        if not available:
            available = [a for a in universe.neutral
                         if a in price_df.columns and not pd.isna(price_df.loc[date, a])]
        if not available:
            # 找任何有價格的資產
            available = [c for c in price_df.columns if not pd.isna(price_df.loc[date, c])][:1]

        equity_weight = strategy.weight(score)
        n = len(available)

        # 等權資產月報酬
        asset_rets = [float(price_ret.loc[date, a])
                      for a in available
                      if not pd.isna(price_ret.loc[date, a])]
        avg_asset_ret = np.mean(asset_rets) if asset_rets else 0.0

        # 換倉成本（換掉的標的比例 × 雙邊成本）
        changed = set(available) ^ set(prev_assets)
        turnover = len(changed) / max(n, len(prev_assets), 1)
        tx_cost = turnover * TX_COST_ONEWAY * 2

        # 組合月報酬
        port_ret = equity_weight * (avg_asset_ret - tx_cost) + (1 - equity_weight) * cash_monthly_yield
        equity = equity * (1 + port_ret)

        mode = "積極" if score >= 60 else ("中性" if score >= 40 else "防禦")

        # 記錄換倉事件
        if set(available) != set(prev_assets) or i == 1:
            trades.append({
                "date":          date.strftime("%Y-%m-%d"),
                "score":         round(score, 1),
                "mode":          mode,
                "assets":        available,
                "prev_assets":   list(prev_assets),
                "equity_weight": round(equity_weight, 3),
            })

        monthly_detail.append({
            "date":          date.strftime("%Y-%m-%d"),
            "score":         round(score, 1),
            "mode":          mode,
            "assets":        available,
            "equity_weight": round(equity_weight * 100, 1),
            "asset_ret_pct": round(avg_asset_ret * 100, 2),
            "port_ret_pct":  round(port_ret * 100, 2),
            "equity":        round(equity, 0),
        })

        prev_assets = available

    equity_s = pd.Series(equity_series)
    return {
        "equity_monthly": equity_s,
        "kpis": _kpis(equity_s),
        "trades": trades,
        "monthly_detail": monthly_detail,
    }


def run_benchmark(benchmark_sym: str, prices: dict[str, pd.Series], init_cash: float = INIT_CASH) -> dict:
    """100% 買進持有單一標的。"""
    price_df = _to_monthly({benchmark_sym: prices[benchmark_sym]}) if benchmark_sym in prices else pd.DataFrame()
    if price_df.empty or benchmark_sym not in price_df.columns:
        return {"equity_monthly": pd.Series(), "kpis": {}}
    s = price_df[benchmark_sym].dropna()
    equity_s = init_cash * s / s.iloc[0]
    return {"equity_monthly": equity_s, "kpis": _kpis(equity_s)}
