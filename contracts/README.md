# Investment contracts

This repository is the canonical owner of `MarketRegimeV1`.

- `available_at` is the earliest timestamp a backtest may consume the regime.
- Raw values and normalized scores are both required, preserving explainability.
- `config_version`, weights, thresholds, and per-source freshness make the result reproducible.
- Breaking changes require a new versioned directory. Do not import Python modules across repositories.

Validate locally:

```bash
pip install "jsonschema>=4.23,<5"
python contract_tests/validate_contracts.py --report artifacts/contract-compatibility.html
```

`invest_backtest` keeps a consumer snapshot of this schema and consumes JSON artifacts only.
