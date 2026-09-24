"""Create or verify a build receipt. Never deploys, restarts or enables trading."""
import argparse
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from trader_engine.operations.release_receipt import build_receipt, save_receipt, verify_receipt


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',required=True,type=Path)
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--output',type=Path)
    group.add_argument('--verify',type=Path)
    args=parser.parse_args(argv)
    try:
        current=build_receipt(json.loads(args.manifest.read_text()))
        if args.verify:
            result=verify_receipt(json.loads(args.verify.read_text()),current)
            print(json.dumps(result));return 0 if result['matches'] else 2
        save_receipt(args.output,current)
        print(json.dumps({'receipt':str(args.output),'eligible_for_staging':current['eligible_for_staging'],
                          'checks_failed':current['checks_failed'],'checks_unverified':current['checks_unverified'],
                          'execution_authorized':False}))
        return 0 if current['eligible_for_staging'] else 2
    except Exception:
        print(json.dumps({'error':'release_receipt_failed','execution_authorized':False}));return 2


if __name__=='__main__':
    raise SystemExit(main())
