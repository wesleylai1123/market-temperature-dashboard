"""把歷史因子值轉成月頻「合成積極度分數」與「目標股票權重」。

與儀表板共用同一套因子設定（cal_min/cal_max/invert/weights 來自根目錄 data.json），
所以回測驗證的就是儀表板上那套燈號邏輯。

防 lookahead：
  * 因子先 resample 到「月底」(月內最後一筆已知值)。
  * 合成分數再整體往後 shift lag_months 個月才拿來交易。

策略解耦：
  * market_composite_score() 只回傳 0–100 分數序列。
  * 呼叫端自行選策略把分數映射成權重（見 strategies.py）。
  * market_target_weight() 保留為便利函式（預設 linear 策略）。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA_JSON = ROOT / "data.json"


def load_config() -> dict:
    return json.loads(DATA_JSON.read_text(encoding="utf-8"))


def score_of(value, cal_min: float, cal_max: float, invert: bool):
    """單因子原始值 → 積極度 0–100（與 index.html 的 scoreOf 一致）。"""
    span = cal_max - cal_min
    if span == 0:
        pct = 0.5
    else:
        pct = (value - cal_min) / span
    pct = pct.clip(0, 1) if hasattr(pct, "clip") else max(0.0, min(1.0, float(pct)))
    aggressiveness = (1 - pct) if invert else pct
    return aggressiveness * 100.0


def market_composite_score(
    market: str,
    factors_df: pd.DataFrame,
    cfg: dict | None = None,
    lag_months: int = 1,
) -> pd.Series:
    """回傳月頻合成積極度分數（0–100），index 為月底日期。

    factors_df: index=date，欄位為各因子原始值(valuation/sentiment/cycle/rate)，
                缺的因子欄位可省略（自動以有值因子重新分配權重）。
    """
    cfg = cfg or load_config()
    mcfg = cfg["markets"][market]["factors"]
    weights = cfg["weights"]

    df = factors_df.copy()
    df.index = pd.to_datetime(df.index)
    monthly = df.resample("ME").last().ffill()

    score_cols, wmap = {}, {}
    for dim, fcfg in mcfg.items():
        if dim not in monthly.columns:
            continue
        col = pd.to_numeric(monthly[dim], errors="coerce")
        score_cols[dim] = score_of(col, fcfg["cal_min"], fcfg["cal_max"], fcfg["invert"])
        wmap[dim] = weights.get(dim, 0.0)

    if not score_cols:
        raise ValueError(f"{market}: 沒有任何可用因子欄位")

    S = pd.DataFrame(score_cols)
    W = pd.Series(wmap)
    mask = S.notna()
    weighted = (S.fillna(0.0) * W).sum(axis=1)
    wsum = (mask * W).sum(axis=1).replace(0.0, np.nan)
    composite = weighted / wsum  # 0–100

    return composite.shift(lag_months).dropna()


def market_target_weight(
    market: str,
    factors_df: pd.DataFrame,
    cfg: dict | None = None,
    lag_months: int = 1,
    strategy: str = "linear",
) -> pd.Series:
    """便利函式：合成分數 → 目標權重（0–1）。預設使用 linear 策略。"""
    from strategies import get as get_strategy
    scores = market_composite_score(market, factors_df, cfg, lag_months)
    strat = get_strategy(strategy)
    return strat.apply_series(scores).clip(0, 1)
