"""Explicit paper-only cyclic execution experiment, bounded by today's close."""
from __future__ import annotations
import argparse
from dataclasses import asdict
from datetime import datetime,timezone,timedelta
from decimal import Decimal,ROUND_CEILING,ROUND_FLOOR
import hashlib,json,os,shutil,signal,sys,tempfile,time
from pathlib import Path
from urllib.request import Request,build_opener,HTTPRedirectHandler
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from trader_engine.execution.alpaca_paper import AlpacaPaperClient
from trader_engine.execution.journal import ReservedJournal,ExecutionLock
from trader_engine.execution.lifecycle import PaperExecutor,TradePlan
from trader_engine.execution.sizing import PaperSizingConfig,size_long_entry
SYMBOLS=['SPY','QQQ','NVDA','AMD','GOOGL','AMZN','META','MSFT','AAPL','TSLA']

def utc():return datetime.now(timezone.utc)

def entry_window_open(clock,close,now):
    """Freeze this session; never follow next_close into another trading day."""
    stamp=datetime.fromisoformat(clock['timestamp'])
    broker_close=datetime.fromisoformat(clock['next_close'])
    return bool(clock.get('is_open')) and broker_close==close and stamp<close-timedelta(minutes=2) and now<close-timedelta(minutes=2)

def qualified_quote(q,now):
    try:
        bid,ask=Decimal(str(q['bp'])),Decimal(str(q['ap']))
        age=(now-datetime.fromisoformat(q['t'].replace('Z','+00:00'))).total_seconds()
        if not all(v.is_finite() for v in (bid,ask)):return None
        if not (0<bid<=ask and ask>=1 and 0<=age<=5 and (ask-bid)/((ask+bid)/2)*10000<=10):return None
        limit=(ask*Decimal('1.0002')).quantize(Decimal('.01'),rounding=ROUND_CEILING)
        stop=(limit*Decimal('.995')).quantize(Decimal('.01'),rounding=ROUND_FLOOR)
        if stop<=0 or stop>=limit:return None
        return limit,stop
    except (ValueError,KeyError,TypeError,ArithmeticError):return None

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def latest_quotes():
    headers={'APCA-API-KEY-ID':os.environ['APCA_API_KEY_ID'],'APCA-API-SECRET-KEY':os.environ['APCA_API_SECRET_KEY']}
    req=Request('https://data.alpaca.markets/v2/stocks/quotes/latest?symbols='+','.join(SYMBOLS)+'&feed=iex',headers=headers)
    with build_opener(NoRedirect).open(req,timeout=10) as r:return json.load(r)['quotes']

def emit(payload):
    try:print(json.dumps(payload,default=str),flush=True)
    except (OSError,BrokenPipeError):pass

def save(path,obj):
    tmp=path.with_suffix(path.suffix+'.tmp')
    with tmp.open('w') as f:json.dump(obj,f,indent=2,default=str);f.flush();os.fsync(f.fileno())
    tmp.replace(path)

class Control:
    def __init__(self):self.stop=False;self.engine=None
    def halt(self,*args):
        self.stop=True
        if self.engine:self.engine.stop()


def run(out, *, config=None):
    config=config or PaperSizingConfig()
    out=Path(out);out.mkdir(parents=True,exist_ok=False)
    (out/'executor.py').write_text(Path(__file__).read_text())
    control=Control();previous={s:signal.signal(s,control.halt) for s in (signal.SIGINT,signal.SIGTERM)}
    state={'status':'preflight','paper_only':True,'started_at':utc().isoformat(),'target_notional':str(config.target_notional),'max_position_notional':str(config.max_position_notional),'max_position_equity_fraction':str(config.max_position_equity_fraction),'session_start_equity':'100000','session_loss_cutoff':str(config.max_session_loss) if config.max_session_loss is not None else None,'max_concurrent_positions':1,'symbols':SYMBOLS,'feed':'iex','selection':'Fixed cyclic long entries, immediate exits; execution experiment without predictive signal','cycles':0,'completed_round_trips':0,'orders_with_fills':0,'realized_pnl_before_fees':'0','quote_skips':0,'active_journal':None,'active_plan':None,'recovered_cycle':False}
    save(out/'status.json',state)
    tag=hashlib.sha256(os.environ['APCA_API_KEY_ID'].encode()).hexdigest()[:24]
    lock=Path(tempfile.gettempdir())/('trader-engine-paper-'+tag+'.lock')
    try:
        with ExecutionLock(lock),AlpacaPaperClient(allow_orders=True) as broker:
            engine=None
            try:
                account=broker.account();clock=broker.clock()
                close=datetime.fromisoformat(clock['next_close']);cutoff=close-timedelta(minutes=2)
                if not entry_window_open(clock,close,utc()):raise RuntimeError('Todays entry window is closed')
                if broker.positions() or broker.open_orders():raise RuntimeError('Account must be flat at preflight')
                if account.get('status')!='ACTIVE' or account.get('trading_blocked') or account.get('account_blocked'):raise RuntimeError('Paper account blocked')
                state.update(status='running',starting_equity=account['equity'],market_close=close.isoformat(),entry_cutoff=cutoff.isoformat(),last_checked_at=utc().isoformat())
                save(out/'protocol.json',state);save(out/'status.json',state);emit({'event':'START',**state})
                mono_cutoff=time.monotonic()+max(0,(cutoff-datetime.fromisoformat(clock['timestamp'])).total_seconds())
                index=0;cache={};cached_at=0;last_print=0
                while True:
                    state['last_checked_at']=utc().isoformat()
                    if control.stop or (out/'STOP').exists():state['stop_reason']='stop_requested';break
                    clock=broker.clock()
                    if time.monotonic()>=mono_cutoff or not entry_window_open(clock,close,utc()):state['stop_reason']='close_cutoff';break
                    if shutil.disk_usage(out).free<256*1024*1024:state['stop_reason']='low_disk_space';break
                    if broker.positions() or broker.open_orders():raise RuntimeError('Unexpected exposure between cycles')
                    account=broker.account()
                    if account.get('trading_blocked') or account.get('account_blocked'):raise RuntimeError('Account became blocked')
                    if config.max_session_loss is not None and Decimal('100000')-Decimal(account['equity'])>=config.max_session_loss:state['stop_reason']='session_loss_cutoff';break
                    if time.monotonic()-cached_at>=2:
                        cache=latest_quotes();cached_at=time.monotonic()
                        # Local time must actually reach future-stamped data; no timestamp is rewritten.
                        time.sleep(.2)
                    symbol=SYMBOLS[index%len(SYMBOLS)];index+=1
                    prices=qualified_quote(cache.get(symbol,{}),utc())
                    if prices is None:
                        state['quote_skips']+=1;save(out/'status.json',state);continue
                    limit,stop=prices
                    size=size_long_entry(config,equity=account['equity'],cash=account['cash'],session_start_equity='100000',entry_price=limit,stop_price=stop)
                    if not size.allowed:state['stop_reason']=size.rejection_reason;break
                    state['cycles']+=1
                    journal_path=out/(f"cycle_{state['cycles']:05d}.journal")
                    plan=TradePlan.create(symbol,size.quantity,limit)
                    state.update(active_journal=str(journal_path),active_plan=asdict(plan))
                    save(out/'status.json',state) # Flat here; no entry if this write fails.
                    with ReservedJournal(journal_path) as journal:
                        engine=PaperExecutor(broker,journal);control.engine=engine
                        journal.append({'kind':'sizing_quote','symbol':symbol,'sizing':{k:str(v) for k,v in asdict(size).items()},'quote':cache[symbol],'decision_at':utc().isoformat()})
                        def guard():
                            return (not control.stop and not engine.entries_blocked and not engine.stop_requested
                                    and not (out/'STOP').exists() and utc()<cutoff and time.monotonic()<mono_cutoff
                                    and qualified_quote(cache.get(symbol,{}),utc()) is not None
                                    and shutil.disk_usage(out).free>=256*1024*1024)
                        broker.entry_guard=guard
                        result=engine.execute(plan)
                        if result.status=='needs_reconciliation':
                            state['recovered_cycle']=True
                            for _ in range(3):
                                if not broker.clock().get('is_open'):break
                                time.sleep(1)
                                result=engine.resume_drain()
                                if result.status.startswith('flat'):break
                        record={'cycle':state['cycles'],'at':utc().isoformat(),'symbol':symbol,'requested_qty':size.quantity,'limit_price':str(limit),'result':asdict(result)}
                        # Capture complete broker orders even if partial terminal quantities occurred.
                        records=journal.records() if journal.usable else []
                        final_orders={r['order']['id']:r['order'] for r in records if r.get('kind')=='order'}
                        final_orders.update({o['id']:o for o in engine.cache.values()})
                        if result.status.startswith('flat') and Decimal(result.acquired)>0:
                            state['completed_round_trips']+=1
                            state['orders_with_fills']+=sum(Decimal(o.get('filled_qty','0'))>0 for o in final_orders.values())
                        if result.pnl is not None:state['realized_pnl_before_fees']=str(Decimal(state['realized_pnl_before_fees'])+Decimal(result.pnl))
                        state['last_result']=record
                        # Once execute/resume returns, reporting cannot interrupt its exit procedure.
                        with (out/'cycles.jsonl').open('a') as f:f.write(json.dumps(record,default=str)+'\n');f.flush();os.fsync(f.fileno())
                        if result.status!='flat' or result.storage_failed or engine.entries_blocked or state['recovered_cycle']:
                            state['stop_reason']='executor_halted';state['status']='needs_attention' if not result.status.startswith('flat') else 'stopped';break
                    control.engine=None;state['active_plan']=None;state['active_journal']=None
                    state['last_checked_at']=utc().isoformat();save(out/'status.json',state)
                    if time.monotonic()-last_print>30:
                        emit({k:state[k] for k in ['last_checked_at','cycles','completed_round_trips','orders_with_fills','realized_pnl_before_fees']});last_print=time.monotonic()
                if state['status']=='running':state['status']='stopped'
            except Exception as error:
                control.halt();state.update(status='needs_attention',error=str(error))
                # The original engine retains ownership/ambiguity across transient reads.
                if engine is not None and engine.active is not None:
                    try:
                        if broker.clock().get('is_open'):
                            recovery=engine.resume_drain();state['recovery_result']=asdict(recovery)
                    except Exception as recovery_error:state['recovery_error']=str(recovery_error)
            finally:
                control.halt()
                try:
                    account=broker.account();positions=broker.positions();orders=broker.open_orders()
                    state.update(ending_equity=account['equity'],remaining_positions=positions,remaining_open_orders=orders,last_checked_at=utc().isoformat())
                    if 'starting_equity' in state:state['account_change']=str(Decimal(account['equity'])-Decimal(state['starting_equity']))
                    if positions or orders:state['status']='needs_attention'
                    elif state.get('stop_reason')=='close_cutoff' and state['status']!='needs_attention':state['status']='complete'
                except Exception as error:state.update(status='needs_attention',verification_error=str(error))
                state['ended_at']=utc().isoformat()
                try:save(out/'status.json',state)
                except Exception as error:state['reporting_error']=str(error)
                emit({'event':'FINAL',**state})
    finally:
        for sig,handler in previous.items():signal.signal(sig,handler)
    return state

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--execute-paper',action='store_true');p.add_argument('--output',type=Path)
    p.add_argument('--target-notional',default='7500')
    p.add_argument('--max-position-notional',default='10000')
    p.add_argument('--position-equity-fraction',default='0.10')
    p.add_argument('--no-session-loss-limit',action='store_true',help='Explicit paper experiment setting; other sizing/execution limits still apply')
    args=p.parse_args()
    config=PaperSizingConfig(target_notional=args.target_notional,max_position_notional=args.max_position_notional,max_position_equity_fraction=args.position_equity_fraction,max_session_loss=None if args.no_session_loss_limit else '1000')
    if not args.execute_paper:
        emit({'mode':'preview_no_orders','symbols':SYMBOLS,'target_notional':str(config.target_notional),'max_position_notional':str(config.max_position_notional),'max_position_equity_fraction':str(config.max_position_equity_fraction),'entry_cutoff':'verified regular-session close minus 2 minutes','session_loss_cutoff':str(config.max_session_loss) if config.max_session_loss is not None else None,'max_positions':1});return
    if args.output is None:raise ValueError('New output directory required')
    result=run(args.output,config=config)
    if result['status']=='needs_attention':raise SystemExit(2)
if __name__=='__main__':main()
