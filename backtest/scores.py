"""把歷史因子值轉成月頻「目標股票權重」訊號。

與儀表板共用同一套因子設定（cal_min/cal_max/invert/weights 來自根目錄 data.json），
所以回測驗證的就是儀表板上那套燈號邏輯，不會兩邊不一致。

防 lookahead：
* 因子先 resample 到「月底」(月內最後一筆已知值)。
* 合成分數再整體往後 shift 1 個月才拿來交易 —— 即「這個月用上個月底已知的分數」，
  避免用到當月才會知道的資料（總經/基本面還有發布落差，這是保守的下界）。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA_JSON = ROOT / "data.json"


def load_config():
    cfg = json.loads(DATA_JSON.read_text(encoding="utf-8"))
    return cfg


def score_of(value, cal_min, cal_max, invert):
    """單因子原始值 → 積極度 0–100（與 index.html 的 scoreOf 一致）。"""
    span = cal_max - cal_min
    if span == 0:
        pct = 0.5
    else:
        pct = (value - cal_min) / span
    pct = pct.clip(0, 1) if hasattr(pct, "clip") else max(0.0, min(1.0, pct))
    aggressiveness = (1 - pct) if invert else pct
    return aggressiveness * 100.0


def market_target_weight(market: str, factors_df: pd.DataFrame, cfg=None,
                         lag_months: int = 1) -> pd.Series:
    """回傳月頻「目標股票權重」(0–1)，index 為月底日期。

    factors_df: index=date，欄位為各因子原始值(valuation/sentiment/cycle/rate)，
                缺的因子欄位可省略（例如台股 cycle 尚未接線）。
    """
    cfg = cfg or load_config()
    mcfg = cfg["markets"][market]["factors"]
    weights = cfg["weights"]

    df = factors_df.copy()
    df.index = pd.to_datetime(df.index)
    monthly = df.resample("ME").last().ffill()  # 月底最後已知值（月內 ffill）

    # 每個因子各算一條積極度(0–100)；缺資料的月份為 NaN
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
    # 逐月對「當月有值的因子」重新分配權重，避免缺一個因子整月作廢
    mask = S.notna()
    weighted = (S.fillna(0.0) * W).sum(axis=1)
    wsum = (mask * W).sum(axis=1).replace(0.0, np.nan)
    composite = weighted / wsum  # 0–100

    target = (composite / 100.0).clip(0, 1)
    return target.shift(lag_months).dropna()  # 防 lookahead
