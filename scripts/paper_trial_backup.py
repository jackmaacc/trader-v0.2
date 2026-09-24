#!/usr/bin/env python3
"""Create or verify isolated operational-trial backups; never restore live state."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from trader_engine.operations.trial_backup import BackupError, create_backup, restore_backup


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    create = sub.add_parser('create')
    create.add_argument('--source', required=True, type=Path)
    create.add_argument('--destination', required=True, type=Path)
    restore = sub.add_parser('restore')
    restore.add_argument('--source', required=True, type=Path, help='backup directory')
    restore.add_argument('--destination', type=Path, help='NEW isolated exercise directory')
    restore.add_argument('--apply', action='store_true', help='copy to isolated directory; default is verification only')
    args = parser.parse_args(argv)
    try:
        result = create_backup(args.source, args.destination) if args.command == 'create' else restore_backup(args.source, args.destination, dry_run=not args.apply)
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
        return 0
    except BackupError as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}))
        return 2
    except (OSError, ValueError):
        print(json.dumps({'ok': False, 'error': 'backup_operation_failed'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
