"""Audit saved market capabilities without network, credentials or broker actions."""
import argparse
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from trader_engine.operations.capability_audit import build_capability_audit, write_capability_report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifacts', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='New report directory; parent must exist')
    parser.add_argument('--account-snapshot', type=Path, help='Optional saved envelope: checked_at, mode, account')
    args = parser.parse_args(argv)
    try:
        report = build_capability_audit(args.artifacts, account_snapshot=args.account_snapshot)
        write_capability_report(report, args.output)
    except (OSError, ValueError, TypeError):
        print(json.dumps({'error': 'capability_audit_failed', 'execution_authorized': False}))
        return 2
    print(json.dumps({'report_created': True, 'execution_authorized': False, 'live_approved': False}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
