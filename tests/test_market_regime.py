"""Offline checks for dashboard scoring and point-in-time export metadata."""

import copy
import hashlib
import importlib
import json
import math
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

import pytest
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
UPDATED = "2026-10-03T01:17:43Z"
CAPTURED = "2026-10-04T01:00:00Z"
CUTOFF = "2026-10-05T01:00:00Z"


@pytest.fixture
def snapshot():
    def factor(value, **extras):
        return dict(value=value, cal_min=0, cal_max=100, invert=False,
                    stale=False, source_timestamp=UPDATED, **extras)
    return {
        "updated_at": UPDATED,
        "dimensions": ["valuation", "cycle"],
        "weights": {"valuation": 1, "cycle": 3},
        "markets": {"TW": {"factors": {
            "valuation": factor(20), "cycle": factor(80),
        }}, "US": {"factors": {"broken": {"value": True}}}},
    }


def build(data, **overrides):
    try:
        builder = importlib.import_module("market_regime").build_market_regime
    except ModuleNotFoundError as exc:
        if exc.name != "market_regime":
            raise
        pytest.fail("market_regime export is not implemented yet")
    args = dict(market="TW", captured_at=CAPTURED, as_of=CUTOFF)
    args.update(overrides)
    return builder(data, **args)


def test_weighted_scores_and_contract(snapshot):
    original = copy.deepcopy(snapshot)
    result = build(snapshot)
    assert result["raw_factors"] == {"valuation": 20, "cycle": 80}
    assert result["normalized_factor_scores"] == {"valuation": 20, "cycle": 80}
    assert result["factor_weights"] == {"valuation": .25, "cycle": .75}
    assert result["composite_score"] == 65
    assert result["regime"] == "risk_on"
    assert result["industry_states"] == {}
    assert result["thresholds"] == {"risk_on": 60, "risk_off": 40}
    assert result["available_at"] == CAPTURED
    assert result["as_of"] == "2026-10-04"
    assert result["freshness"]["is_stale"] is False
    schema = json.loads((ROOT / "contracts/market-regime/v1/schema.json").read_text())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(result)
    assert snapshot == original  # Pure scoring never changes the input snapshot.


def test_percentile_counts_ties_in_unsorted_reference(snapshot):
    factor = snapshot["markets"]["TW"]["factors"]["valuation"]
    factor.update(value=3, pctile_ref=[8, 3, 1, 3, 2, 9, 4, 5, 6, 7, 10, 11], invert=True)
    assert build(snapshot)["normalized_factor_scores"]["valuation"] == pytest.approx(100 * (1 - 4 / 12))


@pytest.mark.parametrize("value,lo,hi,expected", [(-20, 0, 100, 0), (200, 0, 100, 100),
                                                     (9, 9, 9, 50), (25, 0, 100, 25)])
def test_calibration_fallback_clamps_and_zero_span(snapshot, value, lo, hi, expected):
    factor = snapshot["markets"]["TW"]["factors"]["valuation"]
    factor.update(value=value, cal_min=lo, cal_max=hi, pctile_ref=list(range(11)) + [None, "12"])
    assert build(snapshot)["normalized_factor_scores"]["valuation"] == expected


@pytest.mark.parametrize("score,regime", [(39.999, "risk_off"), (40, "neutral"),
                                           (59.999, "neutral"), (60, "risk_on")])
def test_regime_boundaries(snapshot, score, regime):
    for factor in snapshot["markets"]["TW"]["factors"].values():
        factor["value"] = score
    assert build(snapshot)["regime"] == regime


@pytest.mark.parametrize("values,expected,regime", [
    ((40, 40), 40, "neutral"), ((60, 60), 60, "risk_on"),
    ((12, 60), 40, "neutral"), ((32, 80), 60, "risk_on"),
])
def test_nonbinary_weights_keep_exact_threshold_means(snapshot, values, expected, regime):
    snapshot["weights"] = {"valuation": 5, "cycle": 7}
    for dimension, value in zip(snapshot["dimensions"], values):
        snapshot["markets"]["TW"]["factors"][dimension]["value"] = value
    result = build(snapshot)
    assert result["composite_score"] == expected
    assert result["regime"] == regime


@pytest.mark.parametrize("value,regime", [
    (math.nextafter(40, -math.inf), "risk_off"),
    (math.nextafter(40, math.inf), "neutral"),
    (60 - 2 * math.ulp(60), "neutral"),
    (math.nextafter(60, math.inf), "risk_on"),
])
def test_nonbinary_weights_preserve_adjacent_scores(snapshot, value, regime):
    snapshot["weights"] = {"valuation": 5, "cycle": 7}
    for factor in snapshot["markets"]["TW"]["factors"].values():
        factor["value"] = value
    result = build(snapshot)
    assert result["composite_score"] == value
    assert result["regime"] == regime


@pytest.mark.parametrize("values,threshold,side,regime", [
    ((math.nextafter(12, -math.inf), 60), 40, -1, "risk_off"),
    ((math.nextafter(12, math.inf), 60), 40, 1, "neutral"),
    ((math.nextafter(32, -math.inf), 80), 60, -1, "neutral"),
    ((math.nextafter(32, math.inf), 80), 60, 1, "risk_on"),
])
def test_unequal_scores_adjacent_to_threshold_keep_their_side(snapshot, values, threshold, side, regime):
    snapshot["weights"] = {"valuation": 5, "cycle": 7}
    for dimension, value in zip(snapshot["dimensions"], values):
        snapshot["markets"]["TW"]["factors"][dimension]["value"] = value
    result = build(snapshot)
    assert (result["composite_score"] - threshold) * side > 0
    assert result["regime"] == regime


def test_zero_weight_missing_factor_is_optional(snapshot):
    snapshot["weights"]["cycle"] = 0
    del snapshot["markets"]["TW"]["factors"]["cycle"]
    result = build(snapshot)
    assert result["composite_score"] == 20
    assert result["factor_weights"] == {"valuation": 1, "cycle": 0}
    assert result["raw_factors"] == {"valuation": 20}


def test_large_finite_weights_do_not_overflow(snapshot):
    snapshot["weights"] = {"valuation": 1e308, "cycle": 1e308}
    assert build(snapshot)["composite_score"] == 50


def test_tiny_positive_weight_still_requires_its_factor(snapshot):
    snapshot["weights"] = {"valuation": 1e308, "cycle": 1e-308}
    del snapshot["markets"]["TW"]["factors"]["cycle"]
    with pytest.raises(ValueError):
        build(snapshot)


def test_tiny_positive_weight_still_contributes_to_staleness(snapshot):
    snapshot["weights"] = {"valuation": 1e308, "cycle": 1e-308}
    snapshot["markets"]["TW"]["factors"]["cycle"]["stale"] = True
    assert build(snapshot)["freshness"]["is_stale"] is True


def test_rejects_timezone_with_invalid_offset_minutes(snapshot):
    with pytest.raises(ValueError):
        build(snapshot, captured_at="2026-10-04T01:00:00+00:60")


@pytest.mark.parametrize("invalid", [-1, float("nan"), float("inf"), True, "0.5"])
def test_rejects_bad_weights(snapshot, invalid):
    snapshot["weights"]["cycle"] = invalid
    with pytest.raises(ValueError):
        build(snapshot)


def test_rejects_no_positive_weight(snapshot):
    snapshot["weights"] = {"valuation": 0, "cycle": 0}
    with pytest.raises(ValueError):
        build(snapshot)


@pytest.mark.parametrize("field,invalid", [("value", None), ("value", True), ("value", float("nan")),
    ("value", float("inf")), ("value", "20"), ("cal_min", True), ("cal_max", float("inf")),
    ("invert", "false"), ("stale", "false"), ("pctile_ref", [True]),
    ("pctile_ref", [float("nan")]), ("pctile_ref", [float("inf")])])
def test_rejects_bad_factor_fields(snapshot, field, invalid):
    snapshot["markets"]["TW"]["factors"]["valuation"][field] = invalid
    with pytest.raises(ValueError):
        build(snapshot)


@pytest.mark.parametrize("missing", ["value", "cal_min", "cal_max", "invert", "stale"])
def test_rejects_missing_required_factor_fields(snapshot, missing):
    del snapshot["markets"]["TW"]["factors"]["valuation"][missing]
    with pytest.raises(ValueError):
        build(snapshot)


def test_rejects_missing_positive_weight_factor(snapshot):
    del snapshot["markets"]["TW"]["factors"]["cycle"]
    with pytest.raises(ValueError):
        build(snapshot)


@pytest.mark.parametrize("field", ["updated_at", "captured_at", "as_of", "source_timestamp"])
@pytest.mark.parametrize("invalid", ["2026-10-03", "2026-10-03T00:00:00", "bad", True, None])
def test_requires_timezone_aware_timestamps(snapshot, field, invalid):
    kwargs = {}
    if field in ("captured_at", "as_of"):
        kwargs[field] = invalid
    elif field == "updated_at":
        snapshot[field] = invalid
    else:
        snapshot["markets"]["TW"]["factors"]["valuation"][field] = invalid
    with pytest.raises(ValueError):
        build(snapshot, **kwargs)


def test_rejects_missing_snapshot_timestamp(snapshot):
    del snapshot["updated_at"]
    with pytest.raises(ValueError):
        build(snapshot)


@pytest.mark.parametrize("overrides", [{"as_of": "2026-10-03T01:00:00Z"},
    {"as_of": "2026-10-03T12:00:00Z"}, {"captured_at": "2026-10-03T01:00:00Z"}])
def test_rejects_time_before_snapshot_update_or_capture(snapshot, overrides):
    with pytest.raises(ValueError):
        build(snapshot, **overrides)


def test_rejects_future_source_timestamp(snapshot):
    snapshot["markets"]["TW"]["factors"]["cycle"]["source_timestamp"] = CAPTURED
    with pytest.raises(ValueError):
        build(snapshot)


def test_timestamp_offsets_compare_as_instants_and_output_utc_date(snapshot):
    result = build(snapshot, captured_at="2026-10-04T01:00:00+08:00", as_of="2026-10-04T02:00:00+08:00")
    assert result["available_at"] == "2026-10-03T17:00:00Z"
    assert result["as_of"] == "2026-10-03"


def test_legacy_source_is_stale_even_when_snapshot_flag_is_fresh(snapshot):
    del snapshot["markets"]["TW"]["factors"]["valuation"]["source_timestamp"]
    result = build(snapshot)
    source = next(item for item in result["freshness"]["sources"] if item["source"] == "TW/valuation")
    assert source == dict(source="TW/valuation", source_timestamp=UPDATED, is_stale=True)
    assert result["freshness"]["is_stale"] is True


def test_aggregate_staleness_only_uses_positive_weights(snapshot):
    snapshot["weights"]["cycle"] = 0
    snapshot["markets"]["TW"]["factors"]["cycle"]["stale"] = True
    result = build(snapshot)
    assert result["freshness"]["is_stale"] is False
    assert next(item for item in result["freshness"]["sources"] if item["source"] == "TW/cycle")["is_stale"] is True


def test_config_hash_stable_for_values_metadata_and_key_order(snapshot):
    version = build(snapshot)["config_version"]
    altered = copy.deepcopy(snapshot)
    altered["weights"] = {"cycle": 3, "valuation": 1}
    altered["markets"]["TW"]["factors"]["valuation"].update(value=30, source="renamed", stale=True)
    assert build(altered)["config_version"] == version
    altered["markets"]["TW"]["factors"]["valuation"]["invert"] = True
    assert build(altered)["config_version"] != version


def test_config_hash_distinguishes_exact_weights_that_round_to_same_floats(snapshot):
    first = copy.deepcopy(snapshot)
    first["weights"] = {"valuation": 1e308, "cycle": 0}
    first["markets"]["TW"]["factors"]["valuation"].update(value=40, cal_min=0, cal_max=100, invert=False, pctile_ref=[])
    first["markets"]["TW"]["factors"]["cycle"].update(value=0, cal_min=0, cal_max=100, invert=False, pctile_ref=[])
    second = copy.deepcopy(first)
    second["weights"]["cycle"] = 1e-308
    result1, result2 = build(first), build(second)
    assert result1["factor_weights"] == result2["factor_weights"]
    assert result1["regime"] != result2["regime"]
    assert result1["config_version"] != result2["config_version"]


def test_config_hash_preserves_proportionally_equivalent_exact_weights(snapshot):
    first = build(snapshot)
    second = copy.deepcopy(snapshot)
    second["weights"] = {name: weight * 10**18 for name, weight in snapshot["weights"].items()}
    assert build(second)["config_version"] == first["config_version"]


def test_cli_outputs_deterministic_array_manifest_and_legacy_warning(snapshot, tmp_path):
    del snapshot["markets"]["TW"]["factors"]["valuation"]["source_timestamp"]
    input_path, output = tmp_path / "snapshot.json", tmp_path / "regimes.json"
    raw = (json.dumps(snapshot) + "\n").encode()
    input_path.write_bytes(raw)
    command = [sys.executable, str(ROOT / "export_market_regime.py"), "--input", str(input_path),
               "--market", "TW", "--captured-at", CAPTURED, "--as-of", CUTOFF, "--output", str(output)]
    run = subprocess.run(command, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    payload = output.read_bytes()
    manifest_path = output.with_suffix(".manifest.json")
    manifest_bytes = manifest_path.read_bytes()
    assert json.loads(payload) == [build(snapshot)]
    manifest = json.loads(manifest_bytes)
    assert manifest["raw_input_sha256"] == hashlib.sha256(raw).hexdigest()
    assert manifest["captured_at"] == CAPTURED
    assert manifest["config_version"] == json.loads(payload)[0]["config_version"]
    assert manifest["uncertainty_warnings"][0]["code"] == "LEGACY_SOURCE_TIMESTAMP"
    assert manifest["uncertainty_warnings"][0]["source"] == "TW/valuation"
    assert subprocess.run(command, capture_output=True).returncode == 0
    assert output.read_bytes() == payload
    assert manifest_path.read_bytes() == manifest_bytes


def test_cli_rejects_invalid_input_without_writing_output(snapshot, tmp_path):
    input_path, output = tmp_path / "bad.json", tmp_path / "regimes.json"
    snapshot["markets"]["TW"]["factors"]["valuation"]["value"] = True
    input_path.write_text(json.dumps(snapshot))
    run = subprocess.run([sys.executable, str(ROOT / "export_market_regime.py"), "--input", str(input_path),
        "--market", "TW", "--captured-at", CAPTURED, "--as-of", CUTOFF, "--output", str(output)], capture_output=True)
    assert run.returncode != 0
    assert b"valuation.value" in run.stderr
    assert not output.exists()
    assert not output.with_suffix(".manifest.json").exists()


def test_cli_manifest_directory_does_not_overwrite_previous_output(snapshot, tmp_path):
    input_path, output = tmp_path / "snapshot.json", tmp_path / "regimes.json"
    input_path.write_text(json.dumps(snapshot))
    previous = b"previous successful output\n"
    output.write_bytes(previous)
    output.with_suffix(".manifest.json").mkdir()
    run = subprocess.run([sys.executable, str(ROOT / "export_market_regime.py"), "--input", str(input_path),
        "--market", "TW", "--captured-at", CAPTURED, "--as-of", CUTOFF, "--output", str(output)], capture_output=True)
    assert run.returncode != 0
    assert output.read_bytes() == previous


def test_cli_restores_previous_pair_on_manifest_replace_failure(tmp_path, monkeypatch):
    import export_market_regime
    output = tmp_path / "regimes.json"
    manifest = output.with_suffix(".manifest.json")
    output.write_text("old output")
    manifest.write_text("old manifest")
    real_replace = export_market_regime.os.replace
    calls = 0
    def fail_second(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated manifest write failure")
        real_replace(source, target)
    monkeypatch.setattr(export_market_regime.os, "replace", fail_second)
    with pytest.raises(OSError, match="simulated"):
        export_market_regime._write_pair(output, b"new output", b"new manifest")
    assert output.read_text() == "old output"
    assert manifest.read_text() == "old manifest"
    assert not list(tmp_path.glob(".*.tmp"))


def test_fetch_success_then_failure_preserves_value_source_timestamp(snapshot, monkeypatch, tmp_path):
    import fetch_data
    data_path = tmp_path / "data.json"
    data_path.write_text(json.dumps(snapshot), encoding="utf-8")
    monkeypatch.setattr(fetch_data, "DATA_PATH", data_path)
    monkeypatch.setattr(fetch_data, "FETCHERS", {("TW", "valuation"): lambda: 42})
    before = datetime.now(timezone.utc).replace(microsecond=0)
    fetch_data.main()
    after = datetime.now(timezone.utc)
    first = json.loads(data_path.read_text(encoding="utf-8"))["markets"]["TW"]["factors"]["valuation"]
    fetched = datetime.fromisoformat(first["source_timestamp"].replace("Z", "+00:00"))
    assert before <= fetched <= after
    assert first["value"] == 42
    assert first["stale"] is False
    def failure():
        raise RuntimeError("offline failure")
    monkeypatch.setattr(fetch_data, "FETCHERS", {("TW", "valuation"): failure})
    fetch_data.main()
    second = json.loads(data_path.read_text(encoding="utf-8"))["markets"]["TW"]["factors"]["valuation"]
    assert second["value"] == 42
    assert second["source_timestamp"] == first["source_timestamp"]
    assert second["stale"] is True
