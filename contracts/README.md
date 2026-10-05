# Investment contracts

This repository is the canonical owner of `MarketRegimeV1`.

- `available_at` is the earliest timestamp a backtest may consume the regime.
- Raw values and normalized scores are both required, preserving explainability.
- `config_version`, weights, thresholds, and per-source freshness make the result reproducible.
- Breaking changes require a new versioned directory. Do not import Python modules across repositories.

Validate locally:

```bash
pip install "jsonschema>=4.23,<5" "rfc3339-validator==0.1.4"
python contract_tests/validate_contracts.py --report artifacts/contract-compatibility.html
```

`invest_backtest` keeps a consumer snapshot of this schema and consumes JSON artifacts only.

## Offline regime export

```bash
python export_market_regime.py --input data.json --market TW \
  --captured-at 2026-10-04T01:00:00Z --as-of 2026-10-05T01:00:00Z \
  --output artifacts/market-regimes.json
python -m pytest tests/test_market_regime.py -q
```

The exporter uses only the standard library. It writes a one-record JSON array
and `artifacts/market-regimes.manifest.json`. The manifest contains the SHA256 of
the exact input bytes, capture timestamp, scoring configuration hash, and
structured `uncertainty_warnings` (`code`, `source`, `message`). Git revisions
are recorded by the consuming run separately. Neither command fetches data.

`build_market_regime(data, *, market, captured_at, as_of)` returns a V1 record
without changing the input. Scoring matches the dashboard: at least 12 numeric
reference samples use the fraction less than or equal to the current value;
otherwise scores use clamped calibration (equal endpoints mean 50), then invert
when configured. Finite nonnegative weights are normalized, with at least one
positive weight required. `risk_on` starts at 60; scores below 40 are `risk_off`.
The configuration hash covers the scoring algorithm, selected market, normalized
weights, weighted-mean method, applicable reference/calibration settings,
inversion and thresholds.
It excludes changing observations and freshness metadata.

The weighted mean uses exact ratios of the finite numeric weights and scores,
avoiding normalization drift at 40 and 60. Classification compares the exact
mean. If conversion to a JSON number would land on a threshold from either
side, the exported score uses the adjacent representable number on its true
side; there is no decimal rounding or tolerance band.

All input timestamps require timezones. Capture cannot predate `updated_at`.
`--as-of` is a cutoff and must be at least both snapshot update and capture time;
it does not backdate a newly captured snapshot. `available_at` is the later of
those timestamps, and the record's `as_of` is its UTC date. Consumers must also
enforce `available_at`, rather than relying on the date alone.

Freshness sources use stable factor IDs such as `TW/valuation`. Each timestamp
is the factor's last successful fetch-completion time, and must not follow the
snapshot update or cutoff. On failure, `fetch_data.py` retains both the previous
value and its timestamp and sets `stale=true`. Legacy factors without a source
timestamp use `updated_at` only as a conservative observation timestamp, are
always stale, and emit `LEGACY_SOURCE_TIMESTAMP` in the manifest. This does not
claim the original value was freshly fetched. Aggregate staleness considers
positive-weight factors only; industry states are currently empty.

The configuration hash includes canonical exact normalized weight ratios.
This distinguishes weights whose exported floating-point values coincide but
whose contributions to an exact threshold comparison differ. Proportionally
equivalent weights retain the same configuration identity.

The exporter preflights both output targets and restores the previous record
file if replacing its manifest fails. A failed pair does not replace the last
successful export. Schema validation requires the RFC3339 checker dependency
above so date-time formats are actually checked.
