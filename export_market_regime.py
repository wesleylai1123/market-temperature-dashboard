#!/usr/bin/env python3
"""Export a dashboard snapshot and provenance manifest without fetching data."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

from market_regime import build_market_regime, _iso_utc, _timestamp


def _write_pair(output, payload, manifest_payload):
    manifest = output.with_suffix(".manifest.json")
    for target in (output, manifest):
        if target.exists() and not target.is_file():
            raise ValueError(f"output target is not a regular file: {target}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staged, backup, replaced = [], None, False
    try:
        for target, content in ((output, payload), (manifest, manifest_payload)):
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp", delete=False) as file:
                file.write(content)
                staged.append(Path(file.name))
        if output.exists():
            with tempfile.NamedTemporaryFile(dir=output.parent, prefix=f".{output.name}.backup.", suffix=".tmp", delete=False) as file:
                backup = Path(file.name)
            shutil.copy2(output, backup)
        os.replace(staged[0], output)
        replaced = True
        os.replace(staged[1], manifest)
    except Exception:
        if replaced:
            if backup is not None:
                os.replace(backup, output)
            else:
                output.unlink()
        raise
    finally:
        for path in staged:
            path.unlink(missing_ok=True)
        if backup is not None:
            backup.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--market", required=True)
    parser.add_argument("--captured-at", required=True)
    parser.add_argument("--as-of", required=True, help="Timezone-aware availability cutoff")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        raw = args.input.read_bytes()
        data = json.loads(raw)
        record = build_market_regime(data, market=args.market, captured_at=args.captured_at, as_of=args.as_of)
        factors = data["markets"][args.market]["factors"]
        warnings = [
            {"code": "LEGACY_SOURCE_TIMESTAMP", "source": f"{args.market}/{dim}",
             "message": "Original fetch time unknown; snapshot updated_at is an observation time and this source is stale."}
            for dim in record["raw_factors"] if "source_timestamp" not in factors[dim]
        ]
        manifest = {
            "raw_input_sha256": hashlib.sha256(raw).hexdigest(),
            "captured_at": _iso_utc(_timestamp(args.captured_at, "captured_at")),
            "config_version": record["config_version"], "uncertainty_warnings": warnings,
        }
        payload = json.dumps([record], ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
        manifest_payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
        manifest_path = args.output.with_suffix(".manifest.json")
        if args.output.resolve() == manifest_path.resolve():
            raise ValueError("output must differ from its .manifest.json sibling")
        if args.input.resolve() in (args.output.resolve(), manifest_path.resolve()):
            raise ValueError("output and manifest must not overwrite the input snapshot")
        _write_pair(args.output, payload.encode("utf-8"), manifest_payload.encode("utf-8"))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
