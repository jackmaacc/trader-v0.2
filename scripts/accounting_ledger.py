#!/usr/bin/env python3
"""Import saved evidence only; no network or service actions."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from trader_engine.operations.accounting_ledger import import_bundle, reconcile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    parser.add_argument('--activities')
    parser.add_argument('--context')
    args = parser.parse_args()
    if bool(args.activities) != bool(args.context):
        parser.error('--activities and --context must be supplied together')
    result = import_bundle(args.database, args.activities, args.context) if args.activities else reconcile(args.database)
    print(json.dumps(result, indent=2))
    return 0 if result['status'] == 'reconciled' else 2


if __name__ == '__main__':
    raise SystemExit(main())
