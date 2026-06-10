"""可插拔標的庫 (Universe)：依市場溫度分數決定持有哪些標的。

設計
----
與 strategies.py 正交：
  strategies → 決定「多少 %」放在股票（0–1 總曝險）
  universe   → 決定「放哪些」標的（積極 / 中性 / 防禦三段各有不同池）

月度再平衡時：
  1. strategy.weight(score)          → 股票總曝險 W
  2. universe.assets_for_score(score)→ 標的池 [A, B, C]
  3. 池內等權重：各標的 W/N，剩餘 (1-W) 為現金

新增自訂 Universe 只需：
    register(Universe("my_u", "US", "說明",
        aggressive=["QQQ"], neutral=["SPY"], defensive=["XLU"], benchmark="SPY"))
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Universe:
    name: str
    market: str          # "US" | "TW"
    description: str
    aggressive: list[str]   # score >= 60
    neutral:    list[str]   # 40 <= score < 60
    defensive:  list[str]   # score < 40
    benchmark:  str = ""    # 買進持有對照標的（用於 KPI 比較）

    def assets_for_score(self, score: float) -> list[str]:
        if score >= 60:
            return list(self.aggressive)
        if score >= 40:
            return list(self.neutral)
        return list(self.defensive)

    def all_symbols(self) -> list[str]:
        seen, out = set(), []
        for sym in self.aggressive + self.neutral + self.defensive + ([self.benchmark] if self.benchmark else []):
            if sym not in seen:
                seen.add(sym)
                out.append(sym)
        return out


# ── 內建 Universe ─────────────────────────────────────────────────────────────

REGISTRY: dict[str, Universe] = {

    # ── 美股 ──────────────────────────────────────────────────────────────────

    "us_spy": Universe(
        "us_spy", "US",
        "美股基準：始終持有 SPY（只驗證 strategy 切換效果）",
        aggressive=["SPY"],
        neutral   =["SPY"],
        defensive =["SPY"],
        benchmark ="SPY",
    ),

    "us_sector": Universe(
        "us_sector", "US",
        "美股板塊輪動：積極→QQQ+XLK，中性→SPY，防禦→XLU+XLV",
        aggressive=["QQQ", "XLK"],   # Nasdaq 100 + 科技板塊
        neutral   =["SPY"],          # 寬基指數
        defensive =["XLU", "XLV"],   # 公用事業 + 醫療保健
        benchmark ="SPY",
    ),

    "us_factor": Universe(
        "us_factor", "US",
        "美股因子輪動：積極→MTUM+QUAL，中性→SPY，防禦→USMV+SPLV",
        aggressive=["MTUM", "QUAL"],  # 動能 + 品質因子
        neutral   =["SPY"],
        defensive =["USMV", "SPLV"],  # 低波動 + 標普低波
        benchmark ="SPY",
    ),

    # ── 台股 ──────────────────────────────────────────────────────────────────

    "tw_0050": Universe(
        "tw_0050", "TW",
        "台股基準：始終持有 0050",
        aggressive=["0050"],
        neutral   =["0050"],
        defensive =["0050"],
        benchmark ="0050",
    ),

    "tw_etf": Universe(
        "tw_etf", "TW",
        "台股 ETF 輪動：積極/中性→0050，防禦→0056（高股息）",
        aggressive=["0050"],
        neutral   =["0050"],
        defensive =["0056"],
        benchmark ="0050",
    ),
}


def register(universe: Universe) -> None:
    """把自訂 Universe 加入登錄表。"""
    REGISTRY[universe.name] = universe


def get(name: str) -> Universe:
    if name not in REGISTRY:
        raise ValueError(f"未知 Universe '{name}'，可用：{sorted(REGISTRY)}")
    return REGISTRY[name]


def list_universes() -> None:
    print(f"{'名稱':<18} {'市場':<6} {'說明'}")
    print("-" * 72)
    for u in REGISTRY.values():
        assets = f"積極:{u.aggressive} / 防禦:{u.defensive}"
        print(f"  {u.name:<16} {u.market:<6} {u.description}")
        print(f"    {assets}")
