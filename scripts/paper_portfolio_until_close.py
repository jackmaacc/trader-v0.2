"""Paper-only concurrent breakout experiment; explicit execution required."""
from __future__ import annotations
import argparse,hashlib,json,os,re,shutil,signal,sys,tempfile,threading,time
from pathlib import Path
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from dataclasses import asdict
from urllib.request import Request,build_opener,HTTPRedirectHandler
from urllib.parse import urlencode
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from trader_engine.data.market_feed import StockFeedConfig
from trader_engine.execution.alpaca_paper import AlpacaPaperClient,RollingRateLimiter
from trader_engine.execution.breakout import candidate,hold_breakout
from trader_engine.execution.journal import ReservedJournal,ExecutionLock
from trader_engine.execution.lifecycle import PaperExecutor,TradePlan
from trader_engine.execution.portfolio import Reservations,SymbolBroker
from trader_engine.execution.sizing import PaperSizingConfig,size_long_entry
from paper_breakout_until_close import qualified_quote,record_quote_decision,fetch_entry_quotes,entry_window_open,save,utc

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

class Market:
    def __init__(self,*,feed='iex'):
        self.feed_config=StockFeedConfig(feed)
        self.limiter=RollingRateLimiter()
    def get(self,path,params=None,*,broker=False,priority=False):
        self.limiter.acquire(entry=not priority)
        origin='https://paper-api.alpaca.markets' if broker else 'https://data.alpaca.markets'
        req=Request(origin+path+('?' + urlencode(params) if params else ''),headers={
            'APCA-API-KEY-ID':os.environ['APCA_API_KEY_ID'],'APCA-API-SECRET-KEY':os.environ['APCA_API_SECRET_KEY']})
        with build_opener(NoRedirect).open(req,timeout=12) as response:
            raw=response.read(32*1024*1024+1)
            if len(raw)>32*1024*1024:raise RuntimeError('Market-data response too large')
            return json.loads(raw)
    def assets(self):
        rows=self.get('/v2/assets',{'status':'active','asset_class':'us_equity'},broker=True)
        if not isinstance(rows,list):raise RuntimeError('Invalid asset list')
        return sorted({r['symbol'] for r in rows if r.get('tradable') and re.fullmatch(r'[A-Za-z0-9_.-]{1,32}',r.get('symbol',''))})
    def quotes(self,symbols):
        return self.get('/v2/stocks/quotes/latest',{'symbols':','.join(symbols),'feed':self.feed_config.feed},priority=True)['quotes']
    def bars(self,symbols):
        now=utc();params={'symbols':','.join(symbols),'timeframe':'1Min','start':(now-timedelta(minutes=25)).isoformat(),'end':now.replace(second=0,microsecond=0).isoformat(),'feed':self.feed_config.feed,'adjustment':'raw','limit':10000,'sort':'asc'}
        result={};tokens=set()
        for _ in range(20):
            data=self.get('/v2/stocks/bars',params)
            for symbol,rows in data.get('bars',{}).items():result.setdefault(symbol,[]).extend(rows)
            token=data.get('next_page_token')
            if not token:return result
            if token in tokens:raise RuntimeError('Repeated bar page token')
            tokens.add(token);params['page_token']=token
        raise RuntimeError('Incomplete bar pagination')
    def shortlist(self,symbols,stop,cutoff,progress):
        ranks={};covered=0;now=utc()
        for start in range(0,len(symbols),200):
            if stop.is_set() or utc()>=cutoff:break
            batch=symbols[start:start+200]
            data=self.get('/v2/stocks/snapshots',{'symbols':','.join(batch),'feed':self.feed_config.feed})
            for symbol,row in data.items():
                if not isinstance(row,dict):continue
                try:
                    day=row['dailyBar'];minute=row['minuteBar']
                    price=Decimal(str(minute['c']));volume=Decimal(str(day['v']))
                    stamp=datetime.fromisoformat(minute['t'].replace('Z','+00:00'))
                    if not price.is_finite() or not volume.is_finite():continue
                    if price>=1 and volume>0 and timedelta(0)<=utc()-stamp<=timedelta(minutes=5):ranks[symbol]=price*volume
                except (KeyError,TypeError,ValueError,ArithmeticError):continue
            covered+=len(batch);progress(covered,len(ranks))
        return sorted(ranks,key=ranks.get,reverse=True)[:200],covered,len(ranks)

def run(out,*,feed='iex'):
    feed_config=StockFeedConfig(feed)
    out=Path(out).resolve();out.mkdir(parents=True,exist_ok=False)
    (out/'executor.py').write_text(Path(__file__).read_text())
    stop=threading.Event();jobs={};guard_lock=threading.RLock();result_lock=threading.Lock()
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:stop.set())
    state={'status':'preflight','paper_only':True,'started_at':utc().isoformat(),'target_notional':'15000','position_count_cap':None,'session_loss_cutoff':None,'leverage':False,'protective_stop_pct':1,'target_pct':2,'max_hold_seconds':900,**feed_config.to_record(),'universe':f'Alpaca active tradable US equities; {feed.upper()} data availability required','bar_shortlist_limit':200,'journal_capacity':2048,'cycles':0,'completed_round_trips':0,'orders_with_fills':0,'realized_pnl_before_fees':'0','signal_checks':0,'quote_skips':0}
    def persist():
        with guard_lock:
            state['last_checked_at']=utc().isoformat()
            state['jobs']={s:{k:v for k,v in j.items() if k not in ('thread','engine')} for s,j in jobs.items()}
            state['active_positions_or_entries']=sum(not j['done'] for j in jobs.values())
            save(out/'status.json',state)
    def progress(covered,available):
        state.update(snapshot_symbols_checked=covered,snapshot_symbols_usable=available);persist()
    def stopped():return stop.is_set() or (out/'STOP').exists()
    def emit(x):
        try:print(json.dumps(x,default=str),flush=True)
        except OSError:pass
    tag=hashlib.sha256(os.environ['APCA_API_KEY_ID'].encode()).hexdigest()[:24]
    lock=Path(tempfile.gettempdir())/('trader-engine-paper-'+tag+'.lock')
    with ExecutionLock(lock),AlpacaPaperClient(allow_orders=False) as observer:
        config=PaperSizingConfig(target_notional='15000',max_position_notional='15000',max_position_equity_fraction='1',max_gross_notional='1000000000',max_gross_equity_fraction='1',max_session_loss=None)
        try:
            account=observer.account();clock=observer.clock();close=datetime.fromisoformat(clock['next_close']);cutoff=close-timedelta(minutes=5)
            if not entry_window_open(clock,close,utc()):raise RuntimeError('Entry window closed')
            if observer.positions() or observer.open_orders():raise RuntimeError('Account must be flat before portfolio ownership')
            if account.get('status')!='ACTIVE' or account.get('trading_blocked') or account.get('account_blocked'):raise RuntimeError('Account blocked')
            ledger=Reservations(min(Decimal(account['cash']),Decimal(account['equity'])))
            state.update(status='running',starting_equity=account['equity'],market_close=close.isoformat(),entry_cutoff=cutoff.isoformat(),flatten_at=(close-timedelta(minutes=2)).isoformat())
            market=Market(feed=feed);symbols=market.assets();state['discovered_symbols']=len(symbols)
            save(out/'universe.json',symbols);save(out/'protocol.json',state);persist();emit({'event':'START',**state})
            def worker(symbol,plan,quote,signal_row,sizing,job,received_at):
                result=None;engine=None;failed=False
                try:
                    with AlpacaPaperClient(allow_orders=True) as broker,ReservedJournal(job['journal'],capacity=2048) as journal:
                        def note(info):
                            with guard_lock:job['hold']=info
                            save(Path(job['status_file']),{k:v for k,v in job.items() if k not in ('thread','engine')})
                        def holding(plan,entry,owner):
                            hold_breakout(plan,entry,owner,get_quote=lambda s:market.quotes([s]).get(s),close_at=close-timedelta(minutes=2),should_stop=stopped,notify=note,quote_feed=feed)
                        engine=PaperExecutor(SymbolBroker(broker,symbol),journal,polls=12,hold_callback=holding)
                        with guard_lock:job['engine']=engine
                        broker.entry_guard=lambda:not stopped() and utc()<cutoff and qualified_quote(quote,utc(),symbol=symbol,received_at=received_at,feed=feed,phase='entry_submission',
                            evidence=lambda r:journal.append(r)) is not None and shutil.disk_usage(out).free>=256*1024*1024
                        journal.append({'kind':'sizing_quote','symbol':symbol,'sizing':{k:str(v) for k,v in asdict(sizing).items()},'quote':quote,'decision_at':utc().isoformat(),'breakout_signal':signal_row})
                        result=engine.execute(plan)
                        if result.status=='needs_reconciliation':
                            failed=True;stop.set()
                            for _ in range(3):
                                if not broker.clock().get('is_open'):break
                                time.sleep(1);result=engine.resume_drain()
                                if result.status.startswith('flat'):break
                        if result.status not in ('flat','not_submitted') or result.storage_failed:failed=True;stop.set()
                        orders={o['id']:o for o in engine.cache.values()}
                        record={'symbol':symbol,'plan':asdict(plan),'result':asdict(result),'ended_at':utc().isoformat(),'hold':job.get('hold'),'orders':list(orders.values())}
                        with result_lock:
                            with (out/'cycles.jsonl').open('a') as f:f.write(json.dumps(record,default=str)+'\n');f.flush();os.fsync(f.fileno())
                            with guard_lock:
                                if result.status.startswith('flat') and Decimal(result.acquired)>0:
                                    state['completed_round_trips']+=1;state['orders_with_fills']+=sum(Decimal(o.get('filled_qty','0'))>0 for o in orders.values())
                                if result.pnl is not None:state['realized_pnl_before_fees']=str(Decimal(state['realized_pnl_before_fees'])+Decimal(result.pnl))
                        if result.status in ('flat','not_submitted') and result.pnl is not None:ledger.release_flat(symbol,result.pnl)
                        else:failed=True;stop.set()
                except Exception as error:
                    failed=True;stop.set()
                    with guard_lock:job['error']=str(error)
                    if engine is not None and engine.active is not None:
                        # Client may already have closed; never invent a successful cleanup.
                        with guard_lock:job['needs_reconciliation']=True
                finally:
                    with guard_lock:
                        job.update(done=True,failed=failed,ended_at=utc().isoformat(),result=asdict(result) if result else None)
                    try:save(Path(job['status_file']),{k:v for k,v in job.items() if k not in ('thread','engine')})
                    except Exception:stop.set()
            seen=set();last_exit={};shortlist=[];universe_at=0;bars_at=0;bars={};last_audit=0
            while True:
                if stopped():stop.set();break
                if shutil.disk_usage(out).free<256*1024*1024:raise RuntimeError('Low disk space')
                with guard_lock:
                    for s,j in jobs.items():
                        if j['done'] and s not in last_exit:last_exit[s]=datetime.fromisoformat(j['ended_at'])
                persist()
                if utc()>=cutoff:state['stop_reason']='entry_cutoff';break
                if time.monotonic()-last_audit>=20:
                    clock=observer.clock()
                    if not entry_window_open(clock,close,utc()):state['stop_reason']='entry_cutoff';break
                    account=observer.account()
                    if account.get('trading_blocked') or account.get('account_blocked'):raise RuntimeError('Account blocked during run')
                    with guard_lock:
                        owned_symbols=set(jobs);owned_ids={cid for j in jobs.values() for cid in [j['plan']['entry_id'],*j['plan']['exit_ids']]}
                    if any(p['symbol'] not in owned_symbols for p in observer.positions()):raise RuntimeError('Unowned account position')
                    if any(o['client_order_id'] not in owned_ids for o in observer.open_orders()):raise RuntimeError('Unowned account order')
                    last_audit=time.monotonic()
                if time.monotonic()-universe_at>=180:
                    shortlist,covered,usable=market.shortlist(symbols,stop,cutoff,progress);universe_at=time.monotonic();bars_at=0
                    state.update(snapshot_symbols_checked=covered,snapshot_symbols_usable=usable,shortlist=shortlist)
                    save(out/'latest_shortlist.json',{'at':utc().isoformat(),'symbols':shortlist,'covered':covered,'usable':usable})
                if stopped():break
                if utc()>=cutoff:continue
                if shortlist and time.monotonic()-bars_at>=20:bars=market.bars(shortlist);bars_at=time.monotonic()
                candidates=[]
                for symbol in shortlist:
                    if symbol in jobs and not jobs[symbol]['done']:continue
                    if symbol in last_exit and utc()-last_exit[symbol]<timedelta(minutes=5):continue
                    row=candidate(bars.get(symbol,[]),utc())
                    if row and (symbol,row['signal_at']) not in seen:candidates.append((symbol,row))
                state['signal_checks']+=1
                for symbol,row in sorted(candidates,key=lambda x:Decimal(x[1]['relative_volume']),reverse=True):
                    if stopped() or utc()>=cutoff:break
                    gross,free=ledger.snapshot()
                    if free<2:break
                    quote_batch,received_at=fetch_entry_quotes(lambda:market.quotes([symbol]),(symbol,),feed=feed,
                        evidence=lambda r:record_quote_decision(out/'quote_decisions.jsonl',r))
                    q=quote_batch.get(symbol);time.sleep(.2)
                    prices=qualified_quote(q,utc(),symbol=symbol,received_at=received_at,feed=feed,
                        evidence=lambda r:record_quote_decision(out/'quote_decisions.jsonl',r))
                    if not prices:state['quote_skips']+=1;continue
                    limit,_=prices
                    if qualified_quote(q,utc(),symbol=symbol,received_at=received_at,feed=feed,signal_row=row,phase='entry_signal',
                        evidence=lambda r:record_quote_decision(out/'quote_decisions.jsonl',r)) is None:state['quote_skips']+=1;continue
                    account=observer.account()
                    size=size_long_entry(config,equity=account['equity'],cash=min(Decimal(account['cash']),free),session_start_equity=state['starting_equity'],entry_price=limit,stop_price=limit*Decimal('.99'),gross_open_notional=gross)
                    if not size.allowed:continue
                    ledger.reserve(symbol,size.notional,size.reserved_cash)
                    plan=TradePlan.create(symbol,size.quantity,limit,entry_tif='day');state['cycles']+=1
                    job={'plan':asdict(plan),'journal':str(out/f"cycle_{state['cycles']:05d}.journal"),'status_file':str(out/f"cycle_{state['cycles']:05d}.json"),'done':False,'started_at':utc().isoformat()}
                    with guard_lock:jobs[symbol]=job
                    last_exit.pop(symbol,None);seen.add((symbol,row['signal_at']));persist()
                    thread=threading.Thread(target=worker,args=(symbol,plan,q,row,size,job,received_at),name='paper-'+symbol,daemon=False)
                    job['thread']=thread;thread.start()
                time.sleep(2)
        except Exception as error:
            stop.set();state.update(status='needs_attention',error=str(error));emit({'event':'ERROR','error':str(error)})
        finally:
            # All workers retain their ownership, stops and journals until they drain.
            while True:
                with guard_lock:alive=[j for j in jobs.values() if j.get('thread') and j['thread'].is_alive()]
                if not alive:break
                if (out/'STOP').exists():stop.set()
                if stop.is_set():
                    for job in alive:
                        if job.get('engine'):job['engine'].stop()
                try:persist()
                except Exception:stop.set()
                for job in alive:job['thread'].join(timeout=.2)
                time.sleep(1)
            try:
                account=observer.account();positions=observer.positions();orders=observer.open_orders()
                state.update(ending_equity=account['equity'],remaining_positions=positions,remaining_open_orders=orders)
                if 'starting_equity' in state:state['account_change']=str(Decimal(account['equity'])-Decimal(state['starting_equity']))
                if positions or orders or any(j.get('failed') for j in jobs.values()):state['status']='needs_attention'
                elif state['status']=='running':state['status']='complete' if state.get('stop_reason')=='entry_cutoff' else 'stopped'
            except Exception as error:state.update(status='needs_attention',verification_error=str(error))
            state['ended_at']=utc().isoformat()
            try:persist()
            except Exception as error:state['reporting_error']=str(error)
            emit({'event':'FINAL',**state})
    return state

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--execute-paper',action='store_true');p.add_argument('--output',type=Path);p.add_argument('--feed',choices=('iex','sip'),default='iex',help='Explicit stock data feed; SIP requires entitlement; no fallback');args=p.parse_args()
    if not args.execute_paper:
        print(json.dumps({'mode':'preview_no_orders',**StockFeedConfig(args.feed).to_record(),'position_count_cap':None,'target_notional':15000,'cash_only':True,'session_loss_cutoff':None,'universe':'Alpaca active tradable US equities','screening':f'{args.feed.upper()} snapshots across universe; completed bars for up to 200 most liquid available symbols','entry_cutoff':'close minus 5 minutes','flatten_at':'close minus 2 minutes'}));return
    if not args.output:raise ValueError('New output directory required')
    state=run(args.output,feed=args.feed)
    if state['status']=='needs_attention':sys.exit(2)
if __name__=='__main__':main()
