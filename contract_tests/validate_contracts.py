from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]


def validate_contracts() -> list[tuple[str, str, str]]:
    results: list[tuple[str, str, str]] = []
    schemas = sorted((ROOT / "contracts").glob("*/v1/schema.json"))
    if not schemas:
        raise RuntimeError("no contract schemas found")

    for schema_path in schemas:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        examples = schema_path.parent / "examples"
        valid_files = sorted(examples.glob("valid*.json"))
        invalid_files = sorted(examples.glob("invalid*.json"))
        if not valid_files or not invalid_files:
            raise RuntimeError(f"{schema_path}: valid and invalid fixtures are required")

        for fixture in valid_files:
            payload = json.loads(fixture.read_text(encoding="utf-8"))
            errors = sorted(validator.iter_errors(payload), key=lambda item: list(item.path))
            if errors:
                raise AssertionError(f"{fixture}: expected valid: {errors[0].message}")
            results.append((schema["title"], fixture.name, "valid"))

        for fixture in invalid_files:
            payload = json.loads(fixture.read_text(encoding="utf-8"))
            if not list(validator.iter_errors(payload)):
                raise AssertionError(f"{fixture}: expected schema rejection")
            results.append((schema["title"], fixture.name, "rejected as expected"))

    return results


def write_report(path: Path, results: list[tuple[str, str, str]]) -> None:
    rows = "\n".join(
        f"<tr><td>{html.escape(contract)}</td><td>{html.escape(fixture)}</td>"
        f"<td class='ok'>{html.escape(status)}</td></tr>"
        for contract, fixture, status in results
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<title>Contract compatibility report</title>"
        "<style>body{font:16px system-ui;margin:40px;color:#172033}"
        "table{border-collapse:collapse;min-width:720px}th,td{padding:10px 14px;"
        "border:1px solid #d7dce5;text-align:left}th{background:#eef2f7}"
        ".ok{color:#087443;font-weight:700}</style></head><body>"
        "<h1>Contract compatibility report</h1>"
        f"<p>{len(results)} fixture checks passed.</p>"
        "<table><thead><tr><th>Contract</th><th>Fixture</th><th>Status</th>"
        f"</tr></thead><tbody>{rows}</tbody></table></body></html>",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    results = validate_contracts()
    if args.report:
        write_report(args.report, results)
    for result in results:
        print("CONTRACT_OK", *result)


if __name__ == "__main__":
    main()
