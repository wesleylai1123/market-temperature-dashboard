"""合併各回測 JSON → 根目錄 backtest_results.json，供儀表板讀取。

用法：
    python backtest/merge_results.py \\
        --us  backtest/results/us_results.json \\
        --tw  backtest/results/tw_results.json \\
        --growth-us backtest/results/us_growth.json \\
        --growth-tw backtest/results/tw_growth.json \\
        --portfolio-us backtest/results/us_portfolio.json \\
        --portfolio-tw backtest/results/tw_portfolio.json \\
        --out backtest_results.json
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def _load_pair(us_path: str | None, tw_path: str | None, label: str) -> dict:
    out: dict = {}
    for mkt, path in [("US", us_path), ("TW", tw_path)]:
        if path and Path(path).exists():
            out[mkt] = json.loads(Path(path).read_text("utf-8"))
            print(f"  ✅ {label}{mkt}: {path}")
    return out


def merge(
    us_path: str | None,
    tw_path: str | None,
    out_path: str,
    growth_us_path: str | None = None,
    growth_tw_path: str | None = None,
    portfolio_us_path: str | None = None,
    portfolio_tw_path: str | None = None,
) -> None:
    existing: dict = {}
    if Path(out_path).exists():
        try:
            existing = json.loads(Path(out_path).read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}

    markets = _load_pair(us_path, tw_path, "")
    existing_markets = existing.get("markets", {})
    for mkt in ("US", "TW"):
        if mkt not in markets:
            if mkt in existing_markets:
                print(f"  ↺ {mkt}: 本次無資料，保留舊結果")
                markets[mkt] = existing_markets[mkt]
            else:
                print(f"  ⚠ {mkt}: 無資料")

    growth = _load_pair(growth_us_path, growth_tw_path, "growth ")
    existing_growth = existing.get("growth", {})
    for mkt in ("US", "TW"):
        if mkt not in growth and mkt in existing_growth:
            growth[mkt] = existing_growth[mkt]

    portfolio = _load_pair(portfolio_us_path, portfolio_tw_path, "portfolio ")
    existing_portfolio = existing.get("portfolio", {})
    for mkt in ("US", "TW"):
        if mkt not in portfolio and mkt in existing_portfolio:
            portfolio[mkt] = existing_portfolio[mkt]

    if not markets:
        print("沒有任何市場結果，略過輸出。")
        return

    combined: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "markets": markets,
    }
    if growth:
        combined["growth"] = growth
    if portfolio:
        combined["portfolio"] = portfolio

    Path(out_path).write_text(
        json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"→ 合併完成：{out_path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--us", default=None)
    ap.add_argument("--tw", default=None)
    ap.add_argument("--growth-us", default=None)
    ap.add_argument("--growth-tw", default=None)
    ap.add_argument("--portfolio-us", default=None)
    ap.add_argument("--portfolio-tw", default=None)
    ap.add_argument("--out", default="backtest_results.json")
    args = ap.parse_args()
    merge(
        args.us, args.tw, args.out,
        args.growth_us, args.growth_tw,
        args.portfolio_us, args.portfolio_tw,
    )


if __name__ == "__main__":
    main()
