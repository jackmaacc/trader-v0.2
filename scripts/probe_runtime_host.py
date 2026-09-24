"""Read-only local runtime-host inspection. Does not approve a PC cutover."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from trader_engine.operations.host_probe import probe_host, save_report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = probe_host(json.loads(args.manifest.read_text()))
        save_report(args.output, report)
        print(json.dumps({'report': str(args.output), 'checks_failed': report['checks_failed'],
                          'checks_unverified': report['checks_unverified'],
                          'execution_authorized': False, 'pc_acceptance_complete': False}))
        return 2 if report['checks_failed'] else 0
    except Exception:
        print(json.dumps({'error': 'host_probe_failed', 'execution_authorized': False,
                          'pc_acceptance_complete': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
