#!/usr/bin/env python3
"""Inspect a portable runtime or render inactive service files; never starts them."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from trader_engine.operations.runtime import preflight, render_service_files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--render', choices=['systemd', 'launchd'])
    parser.add_argument('--output', type=Path, help='new directory for templates only; never a service install directory')
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.manifest.read_text())
        report = preflight(config)
        if args.render:
            if args.output is None: parser.error('--render requires --output')
            output = args.output.expanduser().resolve()
            install_roots = [Path('/etc/systemd'), Path('/usr/lib/systemd'), Path('/lib/systemd'), Path('/Library/LaunchAgents'), Path('/Library/LaunchDaemons'), Path.home()/'.config/systemd', Path.home()/'Library/LaunchAgents', Path.home()/'Library/LaunchDaemons']
            if any(output == root.resolve() or root.resolve() in output.parents for root in install_roots):
                parser.error('render into a review directory, never a service installation directory')
            args.output = output
            if args.output.exists(): parser.error('--output must be a new directory; no existing files are overwritten')
            if not report['ready_for_render']:
                print(json.dumps(report, indent=2)); return 2
            files = render_service_files(config, args.render)
            args.output.mkdir(parents=True, exist_ok=False)
            for name, content in files.items():
                (args.output/name).write_text(content)
            report['rendered_files'] = [str(args.output/name) for name in files]
        print(json.dumps(report, indent=2))
        return 0 if report['ready_for_render'] else 2
    except (OSError, ValueError):
        print(json.dumps({'ready_for_render': False, 'execution_authorized': False, 'error': 'manifest_or_output_unavailable'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
