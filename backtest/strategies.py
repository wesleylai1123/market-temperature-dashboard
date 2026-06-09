"""可插拔策略庫：把合成積極度分數 (0–100) 映射成目標股票權重 (0–1)。

新增策略只需：
  1. 定義一個 fn(score: float, **params) -> float 函式
  2. 呼叫 register(...) 或直接加進 REGISTRY dict

範例：
    from strategies import REGISTRY, register, Strategy

    def my_fn(score, floor=0.1, ceil=0.9):
        return floor + (ceil - floor) * score / 100.0

    register(Strategy("my_strategy", "自訂線性壓縮", my_fn, {"floor": 0.1, "ceil": 0.9}))
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

import numpy as np


@dataclass
class Strategy:
    name: str
    description: str
    fn: Callable[..., float]
    params: dict = field(default_factory=dict)

    def weight(self, score: float) -> float:
        """分數 → 目標權重，結果限制在 [0, 1]。"""
        return float(np.clip(self.fn(score, **self.params), 0.0, 1.0))

    def apply_series(self, scores) -> object:
        """對 pandas Series 逐元素套用，回傳同類型 Series。"""
        return scores.map(self.weight)


# ── 內建策略函式 ──────────────────────────────────────────────────────────────

def _linear(score: float) -> float:
    """分數直接等比例映射，0→0%，100→100%。"""
    return score / 100.0


def _conservative_linear(score: float, floor: float = 0.2, ceil: float = 0.8) -> float:
    """線性但壓縮在 [floor, ceil]，降低換手頻率與最大持倉。"""
    return floor + (ceil - floor) * score / 100.0


def _band(
    score: float,
    low_cut: float = 40.0, high_cut: float = 60.0,
    low_w: float = 0.30, mid_w: float = 0.60, high_w: float = 0.90,
) -> float:
    """三段帶：低分→防禦固定權重，中段→中性，高分→積極固定權重。"""
    if score >= high_cut:
        return high_w
    if score >= low_cut:
        return mid_w
    return low_w


def _threshold(score: float, cut: float = 50.0, high: float = 0.80, low: float = 0.30) -> float:
    """二值切換：分數過門檻→high，否則→low。乾淨但換手激烈。"""
    return high if score >= cut else low


def _sigmoid(score: float, k: float = 0.10, mid: float = 50.0) -> float:
    """S 型曲線，在 mid 附近過渡平滑，極端值趨近 0/1。"""
    return 1.0 / (1.0 + math.exp(-k * (score - mid)))


def _volatility_target(
    score: float,
    floor: float = 0.15, ceil: float = 0.95, gamma: float = 2.0,
) -> float:
    """凸型映射（gamma > 1），低分時快速縮倉，高分時趨近上限。模擬波動度目標法。"""
    pct = score / 100.0
    return float(np.clip(floor + (ceil - floor) * pct ** gamma, floor, ceil))


# ── 策略登錄表 ─────────────────────────────────────────────────────────────────

REGISTRY: dict[str, Strategy] = {
    "linear": Strategy(
        "linear",
        "線性：目標權重 = 分數 / 100",
        _linear,
    ),
    "conservative": Strategy(
        "conservative",
        "保守線性：壓縮在 20%–80%，降低換手",
        _conservative_linear,
        {"floor": 0.20, "ceil": 0.80},
    ),
    "band": Strategy(
        "band",
        "三段帶：<40→30%，40–60→60%，>60→90%",
        _band,
        {"low_cut": 40.0, "high_cut": 60.0, "low_w": 0.30, "mid_w": 0.60, "high_w": 0.90},
    ),
    "threshold": Strategy(
        "threshold",
        "二值切換：分數 ≥ 50 → 80%，否則 → 30%",
        _threshold,
        {"cut": 50.0, "high": 0.80, "low": 0.30},
    ),
    "sigmoid": Strategy(
        "sigmoid",
        "S 型曲線：中點 50，k=0.10，過渡平滑",
        _sigmoid,
        {"k": 0.10, "mid": 50.0},
    ),
    "vol_target": Strategy(
        "vol_target",
        "凸型映射：低分快速縮倉，模擬波動度目標法",
        _volatility_target,
        {"floor": 0.15, "ceil": 0.95, "gamma": 2.0},
    ),
}


def register(strategy: Strategy) -> None:
    """把自訂策略加入登錄表，供 run_backtest.py 呼叫。"""
    REGISTRY[strategy.name] = strategy


def get(name: str) -> Strategy:
    if name not in REGISTRY:
        raise ValueError(f"未知策略 '{name}'，可用：{sorted(REGISTRY)}")
    return REGISTRY[name]


def list_strategies() -> None:
    print(f"{'名稱':<18} {'說明'}")
    print("-" * 60)
    for s in REGISTRY.values():
        print(f"  {s.name:<16} {s.description}")
