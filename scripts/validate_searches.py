"""Validate a searches.yaml without app.yaml, credentials, or eBay.

The deploy build for the private searches repo runs this before copying the
file to GCS (``deploy/searches-cloudbuild.yaml``), so a broken edit fails that
commit's check and the Cloud Run job keeps reading the last good copy. It uses
the same loader as ``watch-searches``, so anything this accepts the watcher
accepts.

    python scripts/validate_searches.py configs/searches.yaml

Needs only ``pydantic`` and ``pyyaml`` with the repo on ``PYTHONPATH`` -- not a
full install -- which keeps the build step to a few seconds. Exit status is 0
when the file is valid, 1 otherwise.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Runnable from a bare source checkout: `python scripts/validate_searches.py`
# puts scripts/ on sys.path, not the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shoebox.models.saved_search import load_searches_file  # noqa: E402


def _format_interval(seconds: float) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{int(seconds // size)}{unit}"
    return f"{int(seconds)}s"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("path", help="searches.yaml to validate")
    args = parser.parse_args(argv)

    try:
        searches = load_searches_file(args.path).resolved()
    except Exception as exc:  # noqa: BLE001 - every failure is a validation failure
        print(f"INVALID {args.path}:\n{exc}", file=sys.stderr)
        return 1

    enabled = sum(1 for s in searches if s.enabled)
    print(f"OK {args.path}: {len(searches)} search(es), {enabled} enabled")
    for s in searches:
        print(
            f"  {s.name:<28} {'enabled' if s.enabled else 'disabled':<8} "
            f"every {_format_interval(s.interval.total_seconds()):<5} "
            f"-> {s.channel or '(default channel)'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
