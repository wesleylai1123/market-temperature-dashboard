"""月度多資產投資組合回測引擎（vectorbt 版）。

核心邏輯
--------
  每月末決策（使用上月底已知分數，避免 lookahead）：
    1. score  ─→ strategy.weight(score)            = 股票總曝險 W
    2. score  ─→ universe.assets_for_score(score)   = 標的池 [A, B, ...]
    3. 池內各標的等權重 W/N，剩餘 1-W 為現金（合成 CASH 欄位，年化 CASH_YIELD_ANNUAL）
    4. 把所有標的 + CASH 組成一個 vectorbt 群組（cash_sharing），用
       target-percent 再平衡模擬，雙邊手續費 TX_COST_ONEWAY 由 vbt 自動處理。

與 run_backtest.py 的差異
-------------------------
  * 本檔處理多資產（含合成 CASH 標的）；run_backtest.py 處理單一標的多策略並列
  * 本檔以月頻計算
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

import vbt_engine as vbe

if TYPE_CHECKING:
    from strategies import Strategy
    from universe import Universe

INIT_CASH = 1_000_000.0
CASH_YIELD_ANNUAL = 0.04   # 假設現金年化 4%（美國貨幣基金水準）
TX_COST_ONEWAY = 0.0005    # 0.05% 單邊（ETF 成本估計）
ANN = 12                   # 月頻年化


def _kpis(equity: pd.Series) -> dict:
    return vbe.extract_kpis(equity, ANN)


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
    price_ret = price_df.pct_change()   # 月報酬率（資訊用，不影響模擬）

    # 合成 CASH 標的：年化 CASH_YIELD_ANNUAL 的累積序列，與其他資產同欄位模擬
    cash_monthly_yield = (1 + CASH_YIELD_ANNUAL) ** (1 / 12) - 1
    cash_series = pd.Series(
        (1 + cash_monthly_yield) ** np.arange(len(common)), index=common, name="CASH",
    )
    price_raw = price_df.copy()
    price_raw["CASH"] = cash_series
    price_filled = price_raw.bfill().ffill()

    weights = pd.DataFrame(0.0, index=common, columns=price_raw.columns)
    weights.iloc[0, weights.columns.get_loc("CASH")] = 1.0  # 第一個月全現金，不交易

    trades = []
    monthly_detail = []
    prev_assets: list[str] = []

    for i in range(1, len(common)):
        date = common[i]
        score = float(score_monthly.iloc[i - 1])   # 上月分數（已 lag）

        # 選標的 & 計算權重
        target_assets = universe.assets_for_score(score)
        available = [a for a in target_assets
                     if a in price_df.columns and not pd.isna(price_df.loc[date, a])]
        if not available:
            available = [a for a in universe.neutral
                         if a in price_df.columns and not pd.isna(price_df.loc[date, a])]
        if not available:
            available = [c for c in price_df.columns if not pd.isna(price_df.loc[date, c])][:1]

        equity_weight = strategy.weight(score)
        n = len(available)
        for a in available:
            weights.loc[date, a] = equity_weight / n
        weights.loc[date, "CASH"] = 1 - equity_weight

        # 等權資產月報酬（資訊用，顯示於 monthly_detail.asset_ret_pct）
        asset_rets = [float(price_ret.loc[date, a])
                      for a in available
                      if not pd.isna(price_ret.loc[date, a])]
        avg_asset_ret = np.mean(asset_rets) if asset_rets else 0.0

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
        })

        prev_assets = available

    # ── vectorbt 模擬：所有標的 + CASH 組成單一群組（cash_sharing）──────────────
    pf = vbe.run_target_percent(
        price_filled, weights, init_cash=init_cash, fees=TX_COST_ONEWAY,
        cash_sharing=True, group_by=True,
    )
    equity_s = pf.value()
    equity_s.name = None

    for i, d in enumerate(monthly_detail, start=1):
        port_ret = equity_s.iloc[i] / equity_s.iloc[i - 1] - 1
        d["port_ret_pct"] = round(float(port_ret * 100), 2)
        d["equity"] = round(float(equity_s.iloc[i]), 0)

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
    equity_s = vbe.run_holding(s, init_cash)
    return {"equity_monthly": equity_s, "kpis": _kpis(equity_s)}
