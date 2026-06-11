"""共用 vectorbt 模擬 + KPI 抽取邏輯（run_backtest.py / portfolio.py 共用）。

設計取捨
--------
vectorbt 的 `.drawdown()` / `.sharpe_ratio()` / `.trades.win_rate()` 等 accessor
在月頻（`MonthEnd` freq）index 上會丟 `freq_to_timedelta` 例外（已實測重現），
且不同頻率（日/月）需要不同的年化參數。因此本模組只用 vectorbt 做「下單模擬」
（target-percent 再平衡 + fees + cash_sharing），KPI 一律從模擬出的 equity 序列
用 numpy/pandas 手算，與既有 `_kpis()` 邏輯一致並擴充。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def run_target_percent(
    price,
    weights,
    init_cash: float = 1_000_000.0,
    fees: float = 0.0,
    cash_sharing: bool = False,
    group_by=None,
):
    """封裝 vbt.Portfolio.from_orders（target-percent 再平衡）。

    price / weights 可為 pd.Series（單資產）或 pd.DataFrame（多資產，欄位需對齊）。
    """
    import vectorbt as vbt

    kwargs = dict(size=weights, size_type="targetpercent", init_cash=init_cash, fees=fees)
    if cash_sharing:
        kwargs["cash_sharing"] = True
    if group_by is not None:
        kwargs["group_by"] = group_by
    return vbt.Portfolio.from_orders(price, **kwargs)


def run_holding(price: pd.Series, init_cash: float = 1_000_000.0) -> pd.Series:
    """100% 買進持有 equity（解析計算，避免 vbt freq 問題）。"""
    price = price.dropna()
    if price.empty:
        return price
    return init_cash * price / price.iloc[0]


def drawdown_series(equity: pd.Series) -> pd.Series:
    """(equity - 歷史新高) / 歷史新高，<=0。"""
    equity = equity.dropna()
    roll_max = equity.cummax()
    return (equity - roll_max) / roll_max


def _max_dd_duration_periods(dd: pd.Series) -> int:
    """水下（dd < 0）最長連續期數。"""
    max_run = run = 0
    for v in (dd < 0).to_numpy():
        run = run + 1 if v else 0
        max_run = max(max_run, run)
    return max_run


def extract_kpis(equity: pd.Series, ann: int) -> dict:
    """equity 序列 → KPI dict。

    ann: 年化期數（日頻=252，月頻=12）。
    """
    equity = equity.dropna()
    if len(equity) < 2:
        return {}

    ret = equity.pct_change().dropna()
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1 if years > 0 else float("nan")
    sharpe = (ret.mean() / ret.std() * np.sqrt(ann)) if ret.std() > 0 else float("nan")

    downside = ret[ret < 0]
    downside_std = downside.std()
    sortino = (
        (ret.mean() / downside_std * np.sqrt(ann))
        if downside_std and downside_std > 0
        else float("nan")
    )

    dd = drawdown_series(equity)
    max_dd = float(dd.min())
    calmar = (cagr / abs(max_dd)) if max_dd < 0 else float("nan")

    win_rate = float((ret > 0).mean())
    dd_periods = _max_dd_duration_periods(dd)
    max_dd_duration = round(dd_periods * (365.25 / ann))

    return {
        "cagr": round(float(cagr), 4),
        "sharpe": round(float(sharpe), 3),
        "sortino": round(float(sortino), 3) if not np.isnan(sortino) else None,
        "calmar": round(float(calmar), 3) if not np.isnan(calmar) else None,
        "max_dd": round(max_dd, 4),
        "max_dd_duration": int(max_dd_duration),
        "win_rate": round(win_rate, 4),
        "final": round(float(equity.iloc[-1]), 2),
        "years": round(years, 2),
    }


def monthly_export(equity: pd.Series) -> tuple[list[str], list[float]]:
    """月底採樣並標準化至起始值 100。"""
    monthly = equity.resample("ME").last().dropna()
    if monthly.empty:
        return [], []
    norm = (monthly / monthly.iloc[0] * 100.0).round(2)
    return ([ts.strftime("%Y-%m-%d") for ts in norm.index], norm.tolist())


def drawdown_monthly_export(equity: pd.Series) -> tuple[list[str], list[float]]:
    """月底回撤（%，<=0），供前端 underwater 圖。"""
    dd = drawdown_series(equity)
    if dd.empty:
        return [], []
    monthly = dd.resample("ME").last().dropna()
    if monthly.empty:
        return [], []
    return ([ts.strftime("%Y-%m-%d") for ts in monthly.index], (monthly * 100).round(2).tolist())


def monthly_returns(equity: pd.Series) -> dict[str, float]:
    monthly = equity.resample("ME").last().dropna()
    ret = monthly.pct_change().dropna()
    return {ts.strftime("%Y-%m"): round(float(r * 100), 2) for ts, r in ret.items()}
