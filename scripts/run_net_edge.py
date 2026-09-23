"""Offline frozen ETF research and advisory workflow; no order capabilities."""
from pathlib import Path
import argparse
from datetime import date
import json
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from trader_engine.workflows.net_edge import freeze_registry,run_research,review_bundle,analyze_snapshot,write_json,artifact_index,run_shadow_snapshot


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    freeze=sub.add_parser('freeze',help='Freeze four development candidates; omitted overhead remains unknown')
    freeze.add_argument('--output',required=True);freeze.add_argument('--monthly-overhead',type=float);freeze.add_argument('--frozen-at')
    research=sub.add_parser('research',help='Run shared replay; short histories are diagnostics, never qualification')
    research.add_argument('--data',required=True);research.add_argument('--registry',required=True);research.add_argument('--output',required=True)
    research.add_argument('--diagnostic-last',type=int,metavar='N')
    review=sub.add_parser('review',help='Review bound fixed-window confirmation evidence')
    review.add_argument('--bundle',required=True);review.add_argument('--as-of',type=date.fromisoformat,required=True);review.add_argument('--output',required=True)
    analysis=sub.add_parser('analysis',help='Run five local advisory specialists on a supplied point-in-time snapshot')
    analysis.add_argument('--input',required=True);analysis.add_argument('--output',required=True)
    shadow=sub.add_parser('shadow',help='Capture diagnostic decisions from local snapshot; no registration or orders')
    shadow.add_argument('--input',required=True);shadow.add_argument('--registry',required=True)
    shadow.add_argument('--candidate',choices=['MR30','MR60','MOM20','MOM60'],required=True);shadow.add_argument('--output',required=True)
    args=parser.parse_args(argv)
    try:
        if args.command=='freeze':
            target=Path(args.output) if Path(args.output).suffix=='.json' else Path(args.output)/'registry.json'
            result=freeze_registry(target,monthly_overhead=args.monthly_overhead,frozen_at=args.frozen_at)
        elif args.command=='research':result=run_research(args.data,args.registry,args.output,diagnostic_last=args.diagnostic_last)
        elif args.command=='shadow':result=run_shadow_snapshot(args.input,args.registry,args.candidate,args.output)
        else:
            target=Path(args.output)
            if target.suffix!='.json':target=target/('team_report.json' if args.command=='analysis' else 'review.json')
            if target.exists():raise FileExistsError('Choose new output path')
            result=review_bundle(args.bundle,as_of=args.as_of) if args.command=='review' else analyze_snapshot(args.input)
            target.parent.mkdir(parents=True,exist_ok=True);write_json(target,result);artifact_index(target.parent)
        print(json.dumps(dict(command=args.command,output=args.output,approved_for_trading=False,broker_orders_submitted=0)))
        return 0
    except (ValueError,KeyError,TypeError,OSError) as error:
        parser.exit(2,f'Blocked: {error}\n')

if __name__=='__main__':main()
