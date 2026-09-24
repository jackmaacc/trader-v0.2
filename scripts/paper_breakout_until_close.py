"""Explicit paper breakout experiment with broker-held protection and timed exits."""
from __future__ import annotations
import argparse
from dataclasses import asdict
from datetime import datetime,timezone,timedelta
from decimal import Decimal,ROUND_CEILING,ROUND_FLOOR
import hashlib,json,os,shutil,signal,sys,tempfile,time
from pathlib import Path
from urllib.request import Request,build_opener,HTTPRedirectHandler
from urllib.parse import urlencode
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from trader_engine.execution.alpaca_paper import AlpacaPaperClient
from trader_engine.execution.journal import ReservedJournal,ExecutionLock
from trader_engine.execution.lifecycle import PaperExecutor,TradePlan
from trader_engine.execution.sizing import PaperSizingConfig,size_long_entry
from trader_engine.execution.breakout import candidate,hold_breakout
from trader_engine.data.quote_validation import QuoteEnvelope,QuotePolicy,validate_quote
SYMBOLS=['SPY','QQQ','NVDA','AMD','GOOGL','AMZN','META','MSFT','AAPL','TSLA']

def utc():return datetime.now(timezone.utc)

def entry_window_open(clock,close,now):
    """Freeze this session; never follow next_close into another trading day."""
    stamp=datetime.fromisoformat(clock['timestamp'])
    broker_close=datetime.fromisoformat(clock['next_close'])
    return bool(clock.get('is_open')) and broker_close==close and stamp<close-timedelta(minutes=5) and now<close-timedelta(minutes=5)

def record_quote_decision(path,record):
    """Append before acting; serialization/storage failure prohibits the decision."""
    encoded=json.dumps(record,sort_keys=True,allow_nan=False)+'\n'
    with Path(path).open('a') as f:
        f.write(encoded);f.flush();os.fsync(f.fileno())


def fetch_entry_quotes(fetch,symbols,*,evidence,now=utc):
    """Save unavailable entry quote diagnostics without upstream exception text."""
    try:
        quotes=fetch()
    except Exception as exc:
        at=now().isoformat()
        for symbol in symbols:
            record=validate_quote(QuoteEnvelope(symbol,'iex',at,at,None),
                QuotePolicy(expected_feed='iex')).to_record()
            record.update(kind='entry_quote_decision',symbol=symbol,phase='entry_retrieval',
                          valid=False,decision='rejected',retrieval_error=type(exc).__name__,
                          reasons=[*record['reasons'],'quote_retrieval_failed'])
            status=getattr(exc,'status',getattr(exc,'code',None))
            if isinstance(status,int):record['retrieval_status']=status
            evidence(record)
        raise
    return quotes,now().isoformat()


def qualified_quote(q,now,*,symbol='',received_at=None,evidence=None,phase='entry',signal_row=None):
    # These are the existing entry limits, with no size requirement added.
    policy=QuotePolicy(expected_feed='iex',max_source_age_seconds=5,max_cache_age_seconds=5,
                       max_future_skew_seconds=0,max_spread_bps=10,require_sizes=False,min_ask=1)
    envelope=QuoteEnvelope(symbol,'iex',str(received_at or now.isoformat()),now.isoformat(),q)
    validation=validate_quote(envelope,policy,expected_symbol=symbol)
    q=validation.envelope.raw_payload
    record=validation.to_record()
    # Receipt diagnostics are evidence; the existing entry policy gates source
    # age only and permits a previously future quote once local time catches up.
    reasons=[r for r in validation.reasons if r not in ('stale_cache','receipt_after_decision','event_after_receipt')]
    record['validation_reasons']=list(validation.reasons);prices=None
    try:
        bid,ask=Decimal(str(q['bp'])),Decimal(str(q['ap']))
        age=(now-datetime.fromisoformat(q['t'].replace('Z','+00:00'))).total_seconds()
        # Retain Decimal arithmetic and the exact legacy eligibility test.
        usable=(all(v.is_finite() for v in (bid,ask)) and
                0<bid<=ask and ask>=1 and 0<=age<=5 and (ask-bid)/((ask+bid)/2)*10000<=10)
        if usable:
            limit=(ask*Decimal('1.0002')).quantize(Decimal('.01'),rounding=ROUND_CEILING)
            stop=(limit*Decimal('.995')).quantize(Decimal('.01'),rounding=ROUND_FLOOR)
            if stop<=0 or stop>=limit:reasons.append('invalid_entry_stop')
            elif signal_row is not None:
                if bid<=Decimal(signal_row['breakout_level']):reasons.append('bid_not_above_breakout')
                if limit>Decimal(signal_row['signal_close'])*Decimal('1.003'):reasons.append('entry_above_signal_band')
                if not reasons:prices=(limit,stop)
            elif not reasons:prices=(limit,stop)
        elif not reasons:reasons.append('legacy_entry_gate_failed')
    except (ValueError,KeyError,TypeError,ArithmeticError,AttributeError):
        if not reasons:reasons.append('invalid_entry_quote')
    record.update(kind='entry_quote_decision',symbol=symbol,phase=phase,valid=prices is not None,
                  reasons=reasons,decision='accepted' if prices is not None else 'rejected')
    if signal_row is not None:record['breakout_signal']=signal_row
    if prices is not None:record.update(limit_price=str(prices[0]),stop_price=str(prices[1]))
    if evidence is not None:evidence(record)
    return prices

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def latest_quotes():
    headers={'APCA-API-KEY-ID':os.environ['APCA_API_KEY_ID'],'APCA-API-SECRET-KEY':os.environ['APCA_API_SECRET_KEY']}
    req=Request('https://data.alpaca.markets/v2/stocks/quotes/latest?symbols='+','.join(SYMBOLS)+'&feed=iex',headers=headers)
    with build_opener(NoRedirect).open(req,timeout=10) as r:return json.load(r)['quotes']


def latest_bars():
    now=utc();headers={'APCA-API-KEY-ID':os.environ['APCA_API_KEY_ID'],'APCA-API-SECRET-KEY':os.environ['APCA_API_SECRET_KEY']}
    params={'symbols':','.join(SYMBOLS),'timeframe':'1Min','start':(now-timedelta(minutes=25)).isoformat(),'end':now.replace(second=0,microsecond=0).isoformat(),'feed':'iex','adjustment':'raw','limit':1000,'sort':'asc'}
    req=Request('https://data.alpaca.markets/v2/stocks/bars?'+urlencode(params),headers=headers)
    with build_opener(NoRedirect).open(req,timeout=10) as r:data=json.load(r)
    if data.get('next_page_token'):raise RuntimeError('Incomplete breakout bar batch')
    return data['bars']

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
    state={'status':'preflight','paper_only':True,'started_at':utc().isoformat(),'target_notional':str(config.target_notional),'max_position_notional':str(config.max_position_notional),'max_position_equity_fraction':str(config.max_position_equity_fraction),'session_start_equity':'100000','session_loss_cutoff':str(config.max_session_loss) if config.max_session_loss is not None else None,'max_concurrent_positions':1,'symbols':SYMBOLS,'feed':'iex','selection':'Unvalidated breakout hypothesis: completed minute close above prior 15-minute high and relative volume >=1.2', 'entry_time_in_force':'day','protective_stop_pct':1,'monitored_target_pct':2,'max_hold_seconds':900,'journal_capacity':1024,'signal_checks':0,'cycles':0,'completed_round_trips':0,'orders_with_fills':0,'realized_pnl_before_fees':'0','quote_skips':0,'active_journal':None,'active_plan':None,'recovered_cycle':False}
    save(out/'status.json',state)
    tag=hashlib.sha256(os.environ['APCA_API_KEY_ID'].encode()).hexdigest()[:24]
    lock=Path(tempfile.gettempdir())/('trader-engine-paper-'+tag+'.lock')
    try:
        with ExecutionLock(lock),AlpacaPaperClient(allow_orders=True) as broker:
            engine=None
            try:
                account=broker.account();clock=broker.clock()
                close=datetime.fromisoformat(clock['next_close']);cutoff=close-timedelta(minutes=5)
                if not entry_window_open(clock,close,utc()):raise RuntimeError('Todays entry window is closed')
                if broker.positions() or broker.open_orders():raise RuntimeError('Account must be flat at preflight')
                if account.get('status')!='ACTIVE' or account.get('trading_blocked') or account.get('account_blocked'):raise RuntimeError('Paper account blocked')
                state.update(status='running',starting_equity=account['equity'],market_close=close.isoformat(),entry_cutoff=cutoff.isoformat(),flatten_at=(close-timedelta(minutes=2)).isoformat(),last_checked_at=utc().isoformat())
                save(out/'protocol.json',state);save(out/'status.json',state);emit({'event':'START',**state})
                mono_cutoff=time.monotonic()+max(0,(cutoff-datetime.fromisoformat(clock['timestamp'])).total_seconds())
                cache={};cached_at=0;last_print=0;bars_at=0;bar_cache={};seen_signals=set();last_exit={}
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
                    if time.monotonic()-bars_at>=20:
                        bar_cache=latest_bars();bars_at=time.monotonic()
                    candidates=[]
                    for ticker in SYMBOLS:
                        signal_row=candidate(bar_cache.get(ticker,[]),utc())
                        if signal_row and (ticker,signal_row['signal_at']) not in seen_signals and utc()>=last_exit.get(ticker,utc()-timedelta(days=1))+timedelta(minutes=5):
                            candidates.append((ticker,signal_row))
                    state['signal_checks']+=1
                    if not candidates:
                        state['waiting_for']='confirmed_breakout';save(out/'status.json',state);time.sleep(5);continue
                    symbol,signal_row=max(candidates,key=lambda item:Decimal(item[1]['relative_volume']))
                    cache,received_at=fetch_entry_quotes(latest_quotes,(symbol,),
                        evidence=lambda r:record_quote_decision(out/'quote_decisions.jsonl',r))
                    cached_at=time.monotonic();time.sleep(.2)
                    prices=qualified_quote(cache.get(symbol),utc(),symbol=symbol,received_at=received_at,
                        evidence=lambda r:record_quote_decision(out/'quote_decisions.jsonl',r))
                    if prices is None:
                        state['quote_skips']+=1;save(out/'status.json',state);continue
                    limit,_=prices
                    if qualified_quote(cache.get(symbol),utc(),symbol=symbol,received_at=received_at,signal_row=signal_row,phase='entry_signal',
                        evidence=lambda r:record_quote_decision(out/'quote_decisions.jsonl',r)) is None:
                        state['quote_skips']+=1;save(out/'status.json',state);time.sleep(2);continue
                    stop=(limit*Decimal('.99')).quantize(Decimal('.01'),rounding=ROUND_FLOOR)
                    size=size_long_entry(config,equity=account['equity'],cash=account['cash'],session_start_equity='100000',entry_price=limit,stop_price=stop)
                    if not size.allowed:state['stop_reason']=size.rejection_reason;break
                    state['cycles']+=1
                    journal_path=out/(f"cycle_{state['cycles']:05d}.journal")
                    plan=TradePlan.create(symbol,size.quantity,limit,entry_tif='day')
                    seen_signals.add((symbol,signal_row['signal_at']))
                    state.update(active_journal=str(journal_path),active_plan=asdict(plan),waiting_for=None)
                    save(out/'status.json',state) # Flat here; no entry if this write fails.
                    with ReservedJournal(journal_path,capacity=1024) as journal:
                        def note_hold(info):
                            state['hold']=info;state['last_checked_at']=utc().isoformat();save(out/'status.json',state)
                        def hold(plan,entry,owner):
                            hold_breakout(plan,entry,owner,get_quote=lambda symbol:latest_quotes().get(symbol),
                                close_at=close-timedelta(minutes=2),should_stop=lambda:control.stop or (out/'STOP').exists(),notify=note_hold)
                        engine=PaperExecutor(broker,journal,polls=12,hold_callback=hold);control.engine=engine
                        journal.append({'kind':'sizing_quote','symbol':symbol,'sizing':{k:str(v) for k,v in asdict(size).items()},'quote':cache[symbol],'decision_at':utc().isoformat(),'breakout_signal':signal_row})
                        def guard():
                            return (not control.stop and not engine.entries_blocked and not engine.stop_requested
                                    and not (out/'STOP').exists() and utc()<cutoff and time.monotonic()<mono_cutoff
                                    and qualified_quote(cache.get(symbol),utc(),symbol=symbol,received_at=received_at,phase='entry_submission',
                                        evidence=lambda r:record_quote_decision(out/'quote_decisions.jsonl',r)) is not None
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
                        if result.status not in ('flat','not_submitted') or result.storage_failed or engine.entries_blocked or state['recovered_cycle']:
                            state['stop_reason']='executor_halted';state['status']='needs_attention' if not result.status.startswith('flat') else 'stopped';break
                    if result.status.startswith('flat') and Decimal(result.acquired)>0:last_exit[symbol]=utc()
                    control.engine=None;state['active_plan']=None;state['active_journal']=None
                    state.pop('hold',None)
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
    p.add_argument('--target-notional',default='15000')
    p.add_argument('--max-position-notional',default='15000')
    p.add_argument('--position-equity-fraction',default='0.20')
    p.add_argument('--no-session-loss-limit',action='store_true',help='Explicit paper experiment setting; other sizing/execution limits still apply')
    args=p.parse_args()
    config=PaperSizingConfig(target_notional=args.target_notional,max_position_notional=args.max_position_notional,max_position_equity_fraction=args.position_equity_fraction,max_session_loss=None if args.no_session_loss_limit else '1000')
    if not args.execute_paper:
        emit({'mode':'preview_no_orders','symbols':SYMBOLS,'target_notional':str(config.target_notional),'max_position_notional':str(config.max_position_notional),'max_position_equity_fraction':str(config.max_position_equity_fraction),'entry_cutoff':'verified regular-session close minus 5 minutes (flatten 2 minutes before close)','session_loss_cutoff':str(config.max_session_loss) if config.max_session_loss is not None else None,'max_positions':1});return
    if args.output is None:raise ValueError('New output directory required')
    result=run(args.output,config=config)
    if result['status']=='needs_attention':raise SystemExit(2)
if __name__=='__main__':main()
