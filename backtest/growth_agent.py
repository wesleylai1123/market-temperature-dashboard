"""成長股選股 Agent（規則式、無需 LLM／API Key，結果可重現、可回測）。

設計理念
--------
這是「量化排名」agent：每月依因子排名候選股，選出 top_n。
不用 LLM 是刻意的 —— 選股邏輯需要在歷史上完全可重現才能回測，
LLM 的非決定性輸出無法滿足這個要求。

因子選擇
--------
TW：月營收年增率 (Monthly Revenue YoY%) —— 台股最經典的成長動能訊號，
    資料源 FinMind TaiwanStockMonthRevenue。月增率公布有時間落差，
    用 lag_months 處理。
US：12-1 月價格動能（排除最近 1 個月，避免短期反轉雜訊）——
    免費資料源無法取得即時財報年增率，動能是常見的成長替代訊號
    （Jegadeesh-Titman 動能因子）。

防 lookahead
------------
TW：revenue 因子 .shift(lag_months)，預設 1 個月（實際公布落差約 10 天，
    1 個月已足夠保守）。
US：12-1 動能本身只用 t-1 與 t-12 的價格，相對決策時點 t 已無未來資訊；
    回測迴圈再用上一期因子值決策本期持股，雙重防呆。

新增市場/候選池
--------------
在 CANDIDATES 加一個 market key 並提供對應 factor_* 方法即可。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

# ── 候選股池（流動性較佳的成長股集中產業）────────────────────────────────────────

CANDIDATES: dict[str, list[str]] = {
    "TW": ["2330", "2454", "3034", "2379", "3008", "2382", "2317", "6669", "3231", "2308"],
    "US": ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO", "AMD", "NFLX"],
}

CANDIDATE_NAMES: dict[str, str] = {
    "2330": "台積電", "2454": "聯發科", "3034": "聯詠", "2379": "瑞昱",
    "3008": "大立光", "2382": "廣達", "2317": "鴻海", "6669": "緯穎",
    "3231": "緯創", "2308": "台達電",
    "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "Nvidia", "GOOGL": "Alphabet",
    "AMZN": "Amazon", "META": "Meta", "TSLA": "Tesla", "AVGO": "Broadcom",
    "AMD": "AMD", "NFLX": "Netflix",
}


@dataclass
class GrowthAgent:
    market: str
    top_n: int = 5
    lag_months: int = 1

    @property
    def candidates(self) -> list[str]:
        if self.market not in CANDIDATES:
            raise ValueError(f"未知市場 '{self.market}'，可用：{sorted(CANDIDATES)}")
        return CANDIDATES[self.market]

    # ── 因子計算 ─────────────────────────────────────────────────────────────

    def factor_tw(self, revenue: dict[str, pd.Series]) -> pd.DataFrame:
        """月營收年增率(%)。index=月底，columns=股票代號。"""
        cols = {}
        for sym, s in revenue.items():
            monthly = s.sort_index().resample("ME").last()
            cols[sym] = monthly.pct_change(12) * 100.0
        df = pd.DataFrame(cols).dropna(how="all")
        return df.shift(self.lag_months)  # 防公布落差 / lookahead

    def factor_us(self, prices: dict[str, pd.Series]) -> pd.DataFrame:
        """12-1 月動能(%) = monthly[t-1] / monthly[t-12] - 1。"""
        cols = {}
        for sym, s in prices.items():
            monthly = s.sort_index().resample("ME").last()
            cols[sym] = (monthly.shift(1) / monthly.shift(12) - 1.0) * 100.0
        return pd.DataFrame(cols).dropna(how="all")

    def factor(self, **data) -> pd.DataFrame:
        """data: TW 需傳 revenue=dict[sym, Series]；US 需傳 prices=dict[sym, Series]。"""
        if self.market == "TW":
            return self.factor_tw(data["revenue"])
        return self.factor_us(data["prices"])

    # ── 選股 ─────────────────────────────────────────────────────────────────

    def monthly_picks(self, factor_df: pd.DataFrame) -> dict[pd.Timestamp, list[dict]]:
        """各月依因子排名取 top_n，回傳 {月底日期: [{symbol, name, factor}, ...]}（高到低）。"""
        picks = {}
        for date, row in factor_df.iterrows():
            ranked = row.dropna().sort_values(ascending=False)
            if ranked.empty:
                continue
            top = ranked.head(self.top_n)
            picks[date] = [
                {"symbol": s, "name": CANDIDATE_NAMES.get(s, s), "factor": round(float(v), 2)}
                for s, v in top.items()
            ]
        return picks
