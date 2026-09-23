"""Preview sizing offline, or explicitly execute ONE guarded paper round trip.

No scheduler, signal selection, background trading, or real-money endpoint.
The optional paper execution flag is off by default.
"""
from __future__ import annotations
import argparse
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from trader_engine.execution.sizing import PaperSizingConfig, size_long_entry
from trader_engine.execution.lifecycle import TradePlan, PaperExecutor


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--symbol',required=True)
    p.add_argument('--limit-price',required=True)
    p.add_argument('--stop-price',required=True,help='Sizing assumption only; immediate-exit experiment does not install a stop order')
    p.add_argument('--equity',default='100000',help='Offline preview equity; execution always reads broker equity')
    p.add_argument('--cash',default='100000',help='Offline preview cash; execution always reads broker cash')
    p.add_argument('--session-start-equity',default='100000',help='Fixed session baseline; do not reset after losses')
    p.add_argument('--target-notional',default='7500')
    p.add_argument('--execute-paper',action='store_true',help='Submit one sized paper-only entry and immediately reconcile/close it')
    p.add_argument('--journal',type=Path,help='New journal file required for explicit paper execution')
    return p


def preview(args, equity, cash):
    config=PaperSizingConfig(target_notional=args.target_notional)
    size=size_long_entry(config,equity=equity,cash=cash,session_start_equity=args.session_start_equity,
                         entry_price=args.limit_price,stop_price=args.stop_price)
    # Validate symbol/precision even for previews; do not print random order IDs.
    if size.allowed:TradePlan.create(args.symbol,size.quantity,args.limit_price)
    return size


def emit(payload):
    try:print(json.dumps(payload,default=str,indent=2),flush=True)
    except (OSError,BrokenPipeError):pass  # Reporting cannot affect cleanup.


def main(argv=None):
    args=parser().parse_args(argv)
    if not args.execute_paper:
        size=preview(args,args.equity,args.cash)
        emit({'mode':'offline_preview_no_orders','symbol':args.symbol,**asdict(size),
              'note':'Supplied account values and modeled stop distance; no market data or broker connection.'})
        return 0
    if args.journal is None or args.journal.exists():
        raise ValueError('Explicit execution requires a NEW --journal path; existing evidence must be reconciled separately')
    from trader_engine.execution.alpaca_paper import AlpacaPaperClient
    from trader_engine.execution.journal import ReservedJournal, ExecutionLock
    key=os.environ.get('APCA_API_KEY_ID')
    if not key:raise ValueError('Missing paper credentials in environment')
    account_tag=hashlib.sha256(key.encode()).hexdigest()[:24]
    lock=Path(tempfile.gettempdir())/('trader-engine-paper-'+account_tag+'.lock')
    with ExecutionLock(lock), AlpacaPaperClient(allow_orders=True) as broker:
        account=broker.account();clock=broker.clock()
        if account.get('status')!='ACTIVE' or account.get('trading_blocked') or account.get('account_blocked'):
            raise ValueError('Paper account is blocked')
        if not clock.get('is_open') or (datetime.fromisoformat(clock['next_close'])-datetime.fromisoformat(clock['timestamp'])).total_seconds()<600:
            raise ValueError('Require at least ten minutes left in the regular session')
        if broker.positions() or broker.open_orders():
            raise ValueError('This immediate-exit runner requires the whole paper account flat')
        size=preview(args,account['equity'],account['cash'])
        if not size.allowed:
            emit({'status':'entry_blocked',**asdict(size)});return 2
        plan=TradePlan.create(args.symbol,size.quantity,args.limit_price)
        with ReservedJournal(args.journal) as journal:
            engine=PaperExecutor(broker,journal)
            broker.entry_guard=lambda: not engine.stop_requested and not engine.entries_blocked
            previous={sig:signal.signal(sig,lambda *_:engine.stop()) for sig in (signal.SIGINT,signal.SIGTERM)}
            try:
                result=engine.execute(plan)
                emit({'mode':'paper_only_single_round_trip',**asdict(result),'journal':str(args.journal)})
                return 0 if result.status.startswith('flat') else 3
            finally:
                for sig,handler in previous.items():signal.signal(sig,handler)

if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        emit({'status':'stopped','error':str(exc)});raise SystemExit(2)
