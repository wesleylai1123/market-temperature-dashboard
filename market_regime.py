"""Pure, dependency-free MarketRegimeV1 scoring for dashboard snapshots."""

from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
import math
import re


THRESHOLDS = {"risk_on": 60, "risk_off": 40}
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z")


def _timestamp(value, field):
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise ValueError(f"{field} must be an ISO timestamp with a timezone")
    if not value.endswith("Z") and (int(value[-5:-3]) >= 24 or int(value[-2:]) >= 60):
        raise ValueError(f"{field} has an invalid timezone offset")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be a valid timezone-aware timestamp") from exc


def _iso_utc(value):
    return value.isoformat().replace("+00:00", "Z")


def _number(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite numeric value")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError(f"{field} must be a finite numeric value")
    return value


def _mapping(value, field):
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _score(factor, dimension):
    value = _number(factor.get("value"), f"{dimension}.value")
    if not isinstance(factor.get("invert"), bool):
        raise ValueError(f"{dimension}.invert must be a boolean")
    reference = factor.get("pctile_ref", [])
    if not isinstance(reference, list):
        raise ValueError(f"{dimension}.pctile_ref must be an array")
    numeric = []
    for item in reference:
        if isinstance(item, (int, float)):
            numeric.append(_number(item, f"{dimension}.pctile_ref"))
    if len(numeric) >= 12:
        # Counting <= includes every tie, regardless of reference ordering.
        percentile = sum(item <= value for item in numeric) / len(numeric)
        config = {"method": "percentile", "pctile_ref": sorted(numeric)}
    else:
        lo = _number(factor.get("cal_min"), f"{dimension}.cal_min")
        hi = _number(factor.get("cal_max"), f"{dimension}.cal_max")
        span = hi - lo
        percentile = .5 if span == 0 else (value - lo) / span
        if math.isnan(percentile):
            raise ValueError(f"{dimension} calibration produced a nonfinite score")
        percentile = max(0, min(1, percentile))
        config = {"method": "calibration", "cal_min": lo, "cal_max": hi}
    score = (1 - percentile if factor["invert"] else percentile) * 100
    if not math.isfinite(score) or not 0 <= score <= 100:
        raise ValueError(f"{dimension} score must be finite and within [0, 100]")
    config["invert"] = factor["invert"]
    return score, config


def build_market_regime(data, *, market, captured_at, as_of):
    """Build one V1 record; ``as_of`` is the latest permitted availability cutoff.

    All times must carry timezones. Source timestamps denote successful fetch
    completion, never snapshot capture. Missing legacy timestamps conservatively
    use the snapshot observation time and always mark that source stale.
    """
    data = _mapping(data, "data")
    updated = _timestamp(data.get("updated_at"), "updated_at")
    captured = _timestamp(captured_at, "captured_at")
    cutoff = _timestamp(as_of, "as_of")
    if captured < updated:
        raise ValueError("captured_at cannot precede snapshot updated_at")
    available = max(updated, captured)
    if cutoff < available:
        raise ValueError("as_of cutoff cannot precede snapshot update or capture")
    if not isinstance(market, str) or not market:
        raise ValueError("market must be a nonempty string")
    markets = _mapping(data.get("markets"), "markets")
    selected = _mapping(markets.get(market), f"markets.{market}")
    factors = _mapping(selected.get("factors"), f"markets.{market}.factors")
    dimensions = data.get("dimensions")
    if (not isinstance(dimensions, list) or not dimensions
            or any(not isinstance(dim, str) or not dim for dim in dimensions)
            or len(set(dimensions)) != len(dimensions)):
        raise ValueError("dimensions must contain unique nonempty factor names")
    configured_weights = _mapping(data.get("weights"), "weights")
    weights = {}
    for dimension in dimensions:
        weight = _number(configured_weights.get(dimension, 0), f"weights.{dimension}")
        if weight < 0:
            raise ValueError(f"weights.{dimension} must be nonnegative")
        weights[dimension] = weight
    largest = max(weights.values())
    if largest == 0:
        raise ValueError("at least one factor weight must be positive")
    positive_factors = {dim for dim, weight in weights.items() if weight > 0}
    # Exact ratios avoid overflow and drift from repeatedly normalizing floats.
    exact_weights = {dim: Fraction(weight) for dim, weight in weights.items()}
    total_weight = sum(exact_weights.values())
    weights = {dim: float(weight / total_weight) for dim, weight in exact_weights.items()}
    exact_weight_config = {}
    for dimension, weight in exact_weights.items():
        ratio = weight / total_weight
        exact_weight_config[dimension] = {"numerator": str(ratio.numerator),
                                          "denominator": str(ratio.denominator)}

    raw, scores, configurations, sources = {}, {}, {}, []
    is_stale = False
    for dimension in dimensions:
        factor = factors.get(dimension)
        if factor is None and dimension not in positive_factors:
            continue
        factor = _mapping(factor, f"factors.{dimension}")
        score, config = _score(factor, dimension)
        if not isinstance(factor.get("stale"), bool):
            raise ValueError(f"{dimension}.stale must be a boolean")
        legacy = "source_timestamp" not in factor
        source_time = updated if legacy else _timestamp(factor["source_timestamp"], f"{dimension}.source_timestamp")
        if source_time > updated or source_time > cutoff:
            raise ValueError(f"{dimension}.source_timestamp cannot follow snapshot update or cutoff")
        stale = factor["stale"] or legacy
        is_stale = is_stale or (dimension in positive_factors and stale)
        raw[dimension] = factor["value"]
        scores[dimension] = score
        configurations[dimension] = config
        sources.append({"source": f"{market}/{dimension}", "source_timestamp": _iso_utc(source_time), "is_stale": stale})

    exact_composite = sum(Fraction(scores[dim]) * exact_weights[dim] for dim in scores) / total_weight
    composite = float(exact_composite)
    # If float serialization lands exactly on a threshold, retain the true side
    # with the adjacent representable float. Never round a range of scores.
    for boundary in THRESHOLDS.values():
        if composite == boundary and exact_composite != boundary:
            composite = math.nextafter(composite, math.inf if exact_composite > boundary else -math.inf)
    regime = "risk_on" if exact_composite >= 60 else "risk_off" if exact_composite < 40 else "neutral"
    configuration = {"algorithm": "dashboard-score-v1", "market": market,
                     "composite_method": "exact-weighted-mean-v1",
                     "exact_factor_weights": exact_weight_config,
                     "factors": configurations, "factor_weights": weights, "thresholds": THRESHOLDS}
    encoded = json.dumps(configuration, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return {
        "contract": "MarketRegimeV1", "schema_version": "1.0.0", "market": market,
        "as_of": available.date().isoformat(), "available_at": _iso_utc(available),
        "raw_factors": raw, "normalized_factor_scores": scores,
        "composite_score": composite, "regime": regime, "industry_states": {},
        "factor_weights": weights, "thresholds": dict(THRESHOLDS),
        "config_version": "sha256:" + hashlib.sha256(encoded).hexdigest(),
        "freshness": {"is_stale": bool(is_stale), "sources": sources},
    }
