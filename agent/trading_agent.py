"""
市場溫度交易代理人 — Market Temperature Trading Agent

讀取 data.json 當前訊號，呼叫 Claude 分析，輸出結構化交易建議到
agent_report.json，並維護 agent_history.json 做為歷史上下文。

用法：
    cd <repo_root>
    ANTHROPIC_API_KEY=sk-... python agent/trading_agent.py
    python agent/trading_agent.py --dry-run   # 不呼叫 API，只印分數
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_JSON = ROOT / "data.json"
REPORT_JSON = ROOT / "agent_report.json"
HISTORY_JSON = ROOT / "agent_history.json"

MODEL = "claude-haiku-4-5-20251001"
MAX_HISTORY_KEPT = 10  # agent_history.json 最多保留幾筆


# ── 分數計算（與 index.html scoreOf 一致）─────────────────────────────────────

def _score_of(value: float, cal_min: float, cal_max: float, invert: bool) -> float:
    span = cal_max - cal_min
    pct = 0.5 if span == 0 else (value - cal_min) / span
    pct = max(0.0, min(1.0, pct))
    return (1.0 - pct) * 100.0 if invert else pct * 100.0


def compute_scores(data: dict) -> tuple[dict, float]:
    """回傳 {市場: {score, factor_scores}} 與整體分數。"""
    weights = data["weights"]
    allocation = data["allocation"]

    market_scores: dict[str, dict] = {}
    for mkt, mdata in data["markets"].items():
        total_w, total_s = 0.0, 0.0
        factor_scores: dict[str, float] = {}
        for dim, f in mdata["factors"].items():
            w = weights.get(dim, 0.0)
            s = _score_of(f["value"], f["cal_min"], f["cal_max"], f["invert"])
            factor_scores[dim] = round(s, 1)
            total_s += s * w
            total_w += w
        market_scores[mkt] = {
            "score": round(total_s / total_w, 1) if total_w > 0 else 50.0,
            "factor_scores": factor_scores,
        }

    us_w = allocation.get("US", 0.5)
    tw_w = allocation.get("TW", 0.5)
    total_alloc = us_w + tw_w
    overall = (
        (market_scores["US"]["score"] * us_w + market_scores["TW"]["score"] * tw_w)
        / total_alloc
    ) if total_alloc > 0 else 50.0

    return market_scores, round(overall, 1)


# ── 訊號文字摘要 ───────────────────────────────────────────────────────────────

def _stance(score: float) -> str:
    if score >= 60:
        return "偏積極"
    if score >= 40:
        return "中性"
    return "偏防禦"


def build_signal_text(data: dict, market_scores: dict, overall: float) -> str:
    lines = [
        "# 市場溫度儀表板 — 當前訊號快照",
        f"更新時間: {data.get('updated_at', '—')}",
        f"整體積極度: {overall}/100 ({_stance(overall)})",
        "",
    ]
    for mkt, mdata in data["markets"].items():
        info = market_scores[mkt]
        sc = info["score"]
        lines.append(f"## {mdata['label']} ({mkt}) 綜合分數: {sc}/100 → {_stance(sc)}")
        for dim, f in mdata["factors"].items():
            s = info["factor_scores"].get(dim, 0)
            bar = "█" * int(s / 10) + "░" * (10 - int(s / 10))
            stale = " ⚠沿用舊值" if f.get("stale") else ""
            lines.append(
                f"  {f['label']}: {f['value']}{f.get('unit','')} "
                f"→ 積極度 {s:.0f}/100 [{bar}]{stale}"
            )
        lines.append("")
    return "\n".join(lines)


# ── 歷史記錄 ──────────────────────────────────────────────────────────────────

def load_history() -> list[dict]:
    if HISTORY_JSON.exists():
        try:
            return json.loads(HISTORY_JSON.read_text("utf-8"))
        except Exception:
            pass
    return []


def _append_history(report: dict) -> None:
    history = load_history()
    history.append({
        "timestamp": report["timestamp"],
        "overall_stance": report["overall_stance"],
        "overall_score": report["signal_snapshot"]["overall_score"],
        "US_target_pct": report["recommendations"]["US"]["target_weight_pct"],
        "TW_target_pct": report["recommendations"]["TW"]["target_weight_pct"],
        "confidence": report.get("confidence", "—"),
    })
    HISTORY_JSON.write_text(
        json.dumps(history[-MAX_HISTORY_KEPT:], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ── Claude 呼叫 ───────────────────────────────────────────────────────────────

_SYSTEM = """\
你是「市場溫度交易代理人」，一個嚴格遵循紀律的逆向投資框架分析師。
你的角色：基於多因子市場溫度訊號，為長期投資人提供月度股票配置建議。

核心規則：
1. 積極度 ≥ 60 → 市場偏便宜/恐懼，提高股票比重（最高 90%）
2. 積極度 40-60 → 中性，按目標配置持有，小幅調整
3. 積極度 < 40  → 市場偏貴/貪婪，降低曝險（最低 20%）
4. target_weight_pct = 建議的「股票占個別市場部位比例」(0–100)
5. ⚠沿用舊值 的因子信度低，置信度調降
6. 調倉幅度盡量控制 5–15%，避免追漲殺跌
7. 若近期連續決策方向一致則維持，避免頻繁翻轉

輸出「僅」包含合法 JSON，格式：
{
  "overall_stance": "積極|中性|防禦",
  "overall_score": <number>,
  "confidence": "高|中|低",
  "analysis": "<2–3句整體市場環境分析>",
  "recommendations": {
    "US": {
      "stance": "積極|中性|防禦",
      "target_weight_pct": <0–100>,
      "reasoning": "<1–2句決策理由>"
    },
    "TW": {
      "stance": "積極|中性|防禦",
      "target_weight_pct": <0–100>,
      "reasoning": "<1–2句決策理由>"
    }
  },
  "risk_notes": "<主要風險或注意事項，1句>",
  "action_items": ["<具體行動1>", "<具體行動2>"]
}"""


def call_claude(signal_text: str, history: list[dict]) -> dict:
    import anthropic  # lazy import — not needed in --dry-run

    history_block = ""
    if history:
        rows = history[-3:]
        history_block = "\n## 最近決策歷史（最新在下）\n"
        for h in rows:
            history_block += (
                f"- {h['timestamp'][:10]}: {h['overall_stance']}"
                f" 分數{h['overall_score']} "
                f"美{h['US_target_pct']}%/台{h['TW_target_pct']}%"
                f" 信心:{h.get('confidence','—')}\n"
            )

    user_msg = signal_text + history_block + "\n請分析並輸出建議 JSON。"

    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )

    raw = msg.content[0].text.strip()
    # Strip markdown code fence if present
    m = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", raw)
    if m:
        raw = m.group(1)

    return json.loads(raw)


# ── 主程式 ────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="市場溫度交易代理人")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="只印分數，不呼叫 Claude API，不寫檔案",
    )
    ap.add_argument(
        "--output", default=str(REPORT_JSON),
        help=f"報告輸出路徑（預設 {REPORT_JSON}）",
    )
    args = ap.parse_args(argv)

    if not DATA_JSON.exists():
        print(f"❌ 找不到 {DATA_JSON}", file=sys.stderr)
        sys.exit(1)

    data = json.loads(DATA_JSON.read_text("utf-8"))
    market_scores, overall = compute_scores(data)

    print(f"整體積極度: {overall}/100  ({_stance(overall)})")
    for mkt, info in market_scores.items():
        label = data["markets"][mkt]["label"]
        print(f"  {label}({mkt}): {info['score']}/100")

    if args.dry_run:
        print("(dry-run) 略過 Claude 呼叫。")
        return

    signal_text = build_signal_text(data, market_scores, overall)
    history = load_history()

    print(f"呼叫 Claude ({MODEL}) 分析中…")
    result = call_claude(signal_text, history)

    result["timestamp"] = datetime.now(timezone.utc).isoformat()
    result["signal_snapshot"] = {
        "overall_score": overall,
        "market_scores": {k: v["score"] for k, v in market_scores.items()},
        "updated_at": data.get("updated_at"),
    }
    result["model"] = MODEL

    out_path = Path(args.output)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    _append_history(result)

    print(f"✅ {out_path} 已更新")
    print(f"   整體立場: {result['overall_stance']}  信心: {result.get('confidence')}")
    print(f"   美股目標: {result['recommendations']['US']['target_weight_pct']}%"
          f"  台股目標: {result['recommendations']['TW']['target_weight_pct']}%")
    print(f"   分析: {result['analysis']}")
    if result.get("action_items"):
        for item in result["action_items"]:
            print(f"   ▸ {item}")


if __name__ == "__main__":
    main()
