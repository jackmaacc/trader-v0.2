"""Create, verify or restore an isolated private bundle of explicitly selected JSON files."""
import argparse
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from trader_engine.operations.recovery_bundle import create_bundle, verify_bundle, restore_bundle


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create')
    create.add_argument('--source-root', type=Path, required=True)
    create.add_argument('--selection', type=Path, required=True)
    create.add_argument('--destination', type=Path, required=True)
    verify = commands.add_parser('verify'); verify.add_argument('--bundle', type=Path, required=True)
    restore = commands.add_parser('restore'); restore.add_argument('--bundle', type=Path, required=True)
    restore.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'create':
            result = create_bundle(args.source_root, json.loads(args.selection.read_text()), args.destination)
        elif args.command == 'verify':
            result = verify_bundle(args.bundle)
        else:
            result = restore_bundle(args.bundle, args.destination)
        print(json.dumps({'operation': args.command, 'verified': True, 'file_count': len(result['files']) if 'files' in result else result['file_count'], 'execution_authorized': False, 'recovery_authorized': False}))
        return 0
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        print(json.dumps({'error': 'recovery_bundle_failed', 'execution_authorized': False, 'recovery_authorized': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
