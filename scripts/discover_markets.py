from __future__ import annotations
import argparse
from datetime import date
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from trader_engine.data.catalog import SOURCES, discover, save_catalog


def main():
    parser=argparse.ArgumentParser(description='Discover instruments and publish explicit market coverage; does not trade or backtest.')
    parser.add_argument('--sources',nargs='*',choices=list(SOURCES),default=list(SOURCES))
    parser.add_argument('--import-file',action='append',default=[],type=Path,help='Validated JSON or CSV broker/vendor instrument directory; repeatable.')
    parser.add_argument('--output',type=Path,default=Path('artifacts/market_catalog/catalog.json'))
    parser.add_argument('--alpaca',action='store_true',help='Read Alpaca paper asset directories using local environment credentials.')
    parser.add_argument('--alpaca-options-through',type=date.fromisoformat,help='Explicit last expiry date for optional paginated options discovery.')
    args=parser.parse_args()
    if args.alpaca_options_through and not args.alpaca:parser.error('--alpaca-options-through requires --alpaca')
    payload=discover(args.sources,args.import_file,alpaca=args.alpaca,options_through=args.alpaca_options_through)
    print(json.dumps({'catalog':str(args.output),'instruments':len(payload['instruments']),'worldwide_complete':False,'coverage':payload['coverage']},indent=2))
    if not payload['instruments']:
        print('No instruments discovered; existing catalog retained.',file=sys.stderr)
        return 2
    save_catalog(payload,args.output)
    return 2 if any(row['status']=='failed' for row in payload['coverage']) else 0


if __name__=='__main__':
    raise SystemExit(main())
