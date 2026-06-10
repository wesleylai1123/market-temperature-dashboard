#!/usr/bin/env python3
"""把 backtest/data/{us,tw}_factors.csv 的歷史值轉成「百分位參考樣本」，寫回根目錄
data.json 的 markets[mkt].factors[dim].pctile_ref，供 index.html 的 scoreOf() 使用。

pctile_ref：該因子歷史月頻數值由小到大排序的陣列。index.html 用「目前值在這個陣列
中的排名 / 陣列長度」當百分位（與 backtest/scores.py 的擴張視窗百分位排名同一套邏輯，
差別只是儀表板永遠用「至今全部歷史」算今天的百分位，而回測逐期用「至今為止」）。

用法：
  cd backtest && python fetch_history.py --start 2010-01-01   # 先產生 data/*_factors.csv
  python update_percentiles.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA_JSON = ROOT / "data.json"
DATA_DIR = Path(__file__).with_name("data")


def percentile_ref(series: pd.Series) -> list[float]:
    monthly = series.resample("ME").last().ffill().dropna()
    return [round(float(v), 4) for v in sorted(monthly.tolist())]


def main():
    cfg = json.loads(DATA_JSON.read_text(encoding="utf-8"))
    updated = []

    for mkt, fname in (("US", "us_factors.csv"), ("TW", "tw_factors.csv")):
        path = DATA_DIR / fname
        if not path.exists():
            print(f"⚠ {path} 不存在，略過 {mkt}")
            continue
        df = pd.read_csv(path, index_col="date", parse_dates=True)
        factors = cfg["markets"][mkt]["factors"]
        for dim, fcfg in factors.items():
            if dim not in df.columns:
                continue
            ref = percentile_ref(df[dim])
            if len(ref) < 12:
                print(f"⚠ {mkt}/{dim} 樣本數 {len(ref)} < 12，略過")
                continue
            fcfg["pctile_ref"] = ref
            updated.append(f"{mkt}/{dim} ({len(ref)} 筆)")

    if not updated:
        print("沒有可更新的百分位參考樣本")
        return

    DATA_JSON.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("✅ 更新 pctile_ref:", ", ".join(updated))


if __name__ == "__main__":
    main()
