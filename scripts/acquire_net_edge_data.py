#!/usr/bin/env python3
"""Read-only acquisition. Environment credentials only; no account/order endpoints."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trader_engine.data.evidence import acquire_dataset, probe_entitlements


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("probe", "minute", "daily", "quotes", "actions", "calendar"))
    parser.add_argument("--output", required=True, type=Path, help="New immutable evidence directory")
    parser.add_argument("--start", help="ISO session date; minute default 2024-01-01, daily/actions 2016-01-01")
    parser.add_argument("--end", help="Inclusive ISO session date; required except for probe")
    parser.add_argument("--feed", choices=("sip", "iex"), default="sip")
    parser.add_argument("--adjustment", choices=("raw", "split", "dividend", "all"), default="raw")
    parser.add_argument("--max-pages", type=int, default=10000)
    args = parser.parse_args(argv)
    if args.kind == "probe":
        result = probe_entitlements(args.output, session=args.start or "2024-01-03")
    else:
        if not args.end:
            parser.error("--end is required")
        if args.kind in {"quotes", "calendar"} and not args.start:
            parser.error("--start is required for quotes and calendar")
        start = args.start or ("2024-01-01" if args.kind == "minute" else "2016-01-01")
        result = acquire_dataset(args.kind, start, args.end, args.output, feed=args.feed,
                                 adjustment=args.adjustment, max_pages=args.max_pages)
        result = {key: result[key] for key in ("kind", "complete", "record_count", "page_count")}
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

