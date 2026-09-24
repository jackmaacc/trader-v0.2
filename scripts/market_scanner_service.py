"""Continuous GET-only market observation. No execution adapter or order method."""
from __future__ import annotations
import argparse
from datetime import datetime,timedelta,timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import quote,urlencode
from urllib.request import Request,build_opener,HTTPRedirectHandler
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from trader_engine.research.market_scanner import FUTURES,observe,futures_snapshot,summarize,FuturesWindowEmpty
from trader_engine.execution.journal import ExecutionLock
from trader_engine.data.market_feed import StockFeedConfig

class ScanError(RuntimeError):pass
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def atomic(path,payload):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.tmp');fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as handle:
        json.dump(payload,handle,allow_nan=False,separators=(',',':'));handle.write('\n');handle.flush();os.fsync(handle.fileno())
    os.replace(temp,path)
    fd=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)

def credentials():
    from trader_engine.operations.credentials import paper_credentials, CredentialError
    try:
        return paper_credentials()
    except CredentialError as exc:
        raise ScanError(str(exc)) from None


class ReadOnlyTransport:
    ORIGINS={'paper':'https://paper-api.alpaca.markets','data':'https://data.alpaca.markets','yahoo':'https://query1.finance.yahoo.com'}
    ALLOWED={'paper':{'/v2/assets','/v2/clock'},'data':{'/v2/stocks/snapshots','/v1beta3/crypto/us/snapshots'},
             'yahoo':{'/v8/finance/chart/'+quote(s,safe='') for s in FUTURES}}
    def __init__(self,key,secret,*,opener=None,sleep=time.sleep,clock=time.monotonic,feed="iex"):
        self.key=key;self.secret=secret;self.opener=opener or build_opener(NoRedirect());self.sleep=sleep;self.clock=clock;self.last=-100.;self.feed=StockFeedConfig(feed).feed
    def get(self,source,path,params=None):
        if source not in self.ALLOWED or path not in self.ALLOWED[source]:raise ScanError('endpoint_not_allowlisted')
        spacing=.2 if self.feed=="sip" and source=="data" else 1.
        wait=max(0,spacing-(self.clock()-self.last))
        if wait:self.sleep(wait)
        self.last=self.clock()
        headers={'Accept':'application/json','User-Agent':'trader-v02-read-only-scanner/1.0'}
        if source!='yahoo':headers.update({'APCA-API-KEY-ID':self.key,'APCA-API-SECRET-KEY':self.secret})
        request=Request(self.ORIGINS[source]+path+('?' +urlencode(params) if params else ''),headers=headers,method='GET')
        try:
            with self.opener.open(request,timeout=15) as response:
                body=response.read(16*1024*1024+1)
                if not 200<=response.status<300 or len(body)>16*1024*1024:raise ScanError('response_invalid_or_oversized')
                return json.loads(body)
        except HTTPError as exc:
            status=exc.code;exc.close();raise ScanError('http_'+str(status)) from None
        except ScanError:raise
        except Exception:raise ScanError('request_failed') from None

class Scanner:
    def __init__(self,transport,directory,*,futures=True,now=None,feed="iex"):
        self.transport=transport;self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True);self.futures=futures;self.feed=StockFeedConfig(feed).feed
        self.now=now or (lambda:datetime.now(timezone.utc))
        self.instruments=self.read('instruments.json',{'records':[],'last_success_at':{},'stale':True})
        latest=self.read('latest.json',{'records':[]})
        self.previous={(r['asset_class'],r['symbol']):r for r in latest['records']}
    def read(self,name,default):
        path=self.directory/name
        if not path.exists():return default
        try:return json.loads(path.read_text())
        except Exception:raise ScanError('saved_scanner_state_invalid') from None
    def refresh_directory(self,status):
        now=self.now();day=now.date().isoformat();records=self.instruments['records'];success=self.instruments.get('last_success_at',{})
        failed=[]
        for kind in ('us_equity','crypto'):
            if success.get(kind,'')[:10]==day:continue
            try:
                data=self.transport.get('paper','/v2/assets',{'status':'active','asset_class':kind})
                if not isinstance(data,list) or any(not isinstance(r,dict) for r in data):raise ScanError('asset_directory_invalid')
                selected=[]
                for row in data:
                    if row.get('status')!='active' or row.get('tradable') is not True:continue
                    name=row.get('symbol')
                    if not isinstance(name,str) or not name or len(name)>64:raise ScanError('asset_symbol_invalid')
                    if kind=='crypto' and not name.endswith('/USD'):continue
                    selected.append(dict(symbol=name,asset_class=kind,name=row.get('name',''),exchange=row.get('exchange'),tradable=True))
                if not selected:raise ScanError('asset_directory_empty')
                if len({r['symbol'] for r in selected})!=len(selected):raise ScanError('asset_directory_duplicates')
                records=[r for r in records if r['asset_class']!=kind]+selected;success[kind]=now.isoformat()
            except ScanError as exc:failed.append(kind+':'+str(exc))
        if self.futures:
            records=[r for r in records if r['asset_class']!='futures']+[dict(symbol=s,asset_class='futures',name='Indicative continuous futures proxy',tradable=False) for s in FUTURES]
        else:records=[r for r in records if r['asset_class']!='futures']
        self.instruments=dict(checked_at=now.isoformat(),last_success_at=success,stale=bool(failed),errors=failed,records=records)
        atomic(self.directory/'instruments.json',self.instruments)
        status['errors'].extend(failed);status['directory_stale']=bool(failed)
    def publish(self,status,records,*,complete=False):
        now=self.now();coverage,movers=summarize(records)
        coverage['active_total']=sum(r.get('currently_active',True) for r in records)
        status.update(checked_at=now.isoformat(),coverage=coverage)
        if complete:
            status['cycle_completed_at']=now.isoformat()
            status['status']='degraded' if status['errors'] or any(r['state'] in ('missing','invalid','unavailable','stale') for r in records) else 'healthy'
        if complete:
            if shutil.disk_usage(self.directory).free<1024**3:raise ScanError('disk_headroom_below_1GiB')
            atomic(self.directory/'latest.json',dict(checked_at=now.isoformat(),records=records,movers=movers,ranking='descriptive_absolute_change_not_a_prediction'))
        atomic(self.directory/'status.json',status)
    def stock_batch(self,symbols,budget,heartbeat=lambda:None,*,extra=False):
        """Isolate forbidden symbols without changing feed; extra calls have a cycle budget."""
        if extra:
            if budget[0]<=0:return {},{s:'http_403_isolation_budget_exhausted' for s in symbols}
            budget[0]-=1
        heartbeat()
        try:
            payload=self.transport.get('data','/v2/stocks/snapshots',{'symbols':','.join(symbols),'feed':self.feed})
            if not isinstance(payload,dict):raise ScanError('snapshot_response_invalid')
            return payload,{}
        except ScanError as exc:
            reason=str(exc)
            if reason!='http_403':
                return {},{s:reason for s in symbols}
            # Retry the original batch once before subdivision; a transient 403 is not
            # evidence of an inaccessible instrument. Retry consumes the same budget.
            if not extra and budget[0]>0:
                return self.stock_batch(symbols,budget,heartbeat,extra=True)
            if len(symbols)<=1:return {},{s:reason for s in symbols}
            midpoint=len(symbols)//2
            left,left_errors=self.stock_batch(symbols[:midpoint],budget,heartbeat,extra=True)
            right,right_errors=self.stock_batch(symbols[midpoint:],budget,heartbeat,extra=True)
            return left|right,left_errors|right_errors

    def cycle(self):
        now=self.now();status=dict(checked_at=now.isoformat(),status='scanning',mode='read_only',cycle_started_at=now.isoformat(),cycle_completed_at=None,progress={'processed':0,'total':0},sources={},errors=[],stock_feed=self.feed,feed_fallback=False)
        atomic(self.directory/'status.json',status)
        if shutil.disk_usage(self.directory).free<1024**3:
            status.update(status='degraded',error='disk_headroom_below_1GiB',errors=['disk_headroom_below_1GiB'])
            atomic(self.directory/'status.json',status);return status
        self.refresh_directory(status)
        isolation_budget=[32]
        universe=self.instruments['records'];status['progress']['total']=len(universe)
        records=[];indices={};active_keys=set()
        for instrument in universe:
            key=(instrument['asset_class'],instrument['symbol']);active_keys.add(key);indices[key]=len(records)
            row=observe(key[1],key[0],None,self.now(),previous=self.previous.get(key),failure='awaiting_current_cycle',feed=self.feed)
            row.update(state='pending',currently_active=True);records.append(row)
        # Keep a bounded, explicitly inactive profile for seven days after directory removal.
        for key,old in self.previous.items():
            if key in active_keys:continue
            retired=dict(old);retired.update(currently_active=False,state='inactive',retained=True)
            retired.setdefault('inactive_since',now.isoformat())
            if datetime.fromisoformat(retired['inactive_since'])>=now-timedelta(days=7) and len(records)<50000:records.append(retired)
        self.publish(status,records)
        market_open=None
        try:
            clock=self.transport.get('paper','/v2/clock')
            if not isinstance(clock,dict) or not isinstance(clock.get('is_open'),bool):raise ScanError('clock_invalid')
            market_open=clock['is_open']
        except ScanError as exc:status['errors'].append('clock:'+str(exc))
        for kind,name in [('us_equity','equities'),('crypto','crypto'),('futures','futures')]:
            symbols=sorted(r['symbol'] for r in universe if r['asset_class']==kind)
            source=dict(status='scanning',error=None,requested=len(symbols),received=0)
            if kind=='us_equity':source['market_open']=market_open;source['feed']=self.feed
            if kind=='futures':source['execution_eligible']=False;source['latency']='unknown'
            status['sources'][name]=source
            if kind=='futures' and not self.futures:
                source['status']='disabled';continue
            if not symbols:
                source.update(status='unavailable',error='no_verified_instruments');status['errors'].append(name+':no_verified_instruments');continue
            step=1 if kind=='futures' else 200
            bad=False
            for offset in range(0,len(symbols),step):
                batch=symbols[offset:offset+step];failure=None;payload={};symbol_failures={};history_range=None
                status['stage']=name+':batch_'+str(offset//step+1)+'_of_'+str((len(symbols)+step-1)//step)
                atomic(self.directory/'status.json',dict(status,checked_at=self.now().isoformat()))
                try:
                    if kind=='us_equity':
                        payload,symbol_failures=self.stock_batch(batch,isolation_budget,lambda:atomic(self.directory/'status.json',dict(status,checked_at=self.now().isoformat())))
                        status['isolation_extra_requests']=32-isolation_budget[0]
                        if symbol_failures:
                            bad=True;source['error']='restricted_or_unavailable_symbols'
                            status['errors'].append(name+':batch_'+str(offset//step)+':'+','.join(sorted(set(symbol_failures.values()))))
                    elif kind=='crypto':
                        response=self.transport.get('data','/v1beta3/crypto/us/snapshots',{'symbols':','.join(batch)})
                        payload=response.get('snapshots',{}) if isinstance(response,dict) else None
                    else:
                        response=self.transport.get('yahoo','/v8/finance/chart/'+quote(batch[0],safe=''),{'interval':'1m','range':'1d'})
                        history_range='1d'
                        try:
                            parsed=futures_snapshot(response)
                        except FuturesWindowEmpty:
                            history_range='5d'
                            source['wider_window_requests']=source.get('wider_window_requests',0)+1
                            response=self.transport.get('yahoo','/v8/finance/chart/'+quote(batch[0],safe=''),{'interval':'1m','range':'5d'})
                            try:parsed=futures_snapshot(response)
                            except FuturesWindowEmpty:raise ScanError('futures_window_has_no_priced_bars') from None
                        payload={batch[0]:parsed}
                    if not isinstance(payload,dict):raise ScanError('snapshot_response_invalid')
                except (ScanError,ValueError,TypeError,KeyError,AttributeError) as exc:
                    failure=str(exc) if isinstance(exc,ScanError) else 'source_payload_invalid';bad=True;source['error']=failure
                    status['errors'].append(name+':batch_'+str(offset//step)+':'+failure)
                for s in batch:
                    value=payload.get(s) if isinstance(payload,dict) else None
                    row=observe(s,kind,value,self.now(),market_open=market_open,previous=self.previous.get((kind,s)),failure=symbol_failures.get(s,failure),feed=self.feed)
                    if kind=='futures':row['history_range_requested']=history_range
                    row['currently_active']=True;records[indices[(kind,s)]]=row
                    if value is not None and not failure and s not in symbol_failures:source['received']+=1
                    if row['state'] in ('missing','invalid','unavailable','stale'):bad=True
                status['progress']['processed']+=len(batch)
                self.publish(status,records)
            source['status']='degraded' if bad else ('closed' if kind=='us_equity' and market_open is False else 'available')
        status['stage']='cycle_complete'
        self.publish(status,records,complete=True)
        self.previous={(r['asset_class'],r['symbol']):r for r in records}
        history=self.directory/'history';history.mkdir(exist_ok=True)
        atomic(history/(self.now().date().isoformat()+'.json'),dict(status=status,records=records,history_kind='last_complete_scan_per_UTC_day'))
        cutoff=self.now().date()-timedelta(days=6)
        for path in history.glob('*.json'):
            try:day=datetime.strptime(path.stem,'%Y-%m-%d').date()
            except ValueError:continue
            if day<cutoff:path.unlink()
        return status

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--directory',type=Path,default=ROOT/'artifacts/continuous_market_scan');parser.add_argument('--once',action='store_true');parser.add_argument('--poll-seconds',type=float,default=None);parser.add_argument('--feed',choices=['iex','sip'],default='iex');parser.add_argument('--no-futures',action='store_true');args=parser.parse_args(argv)
    if args.poll_seconds is None:args.poll_seconds=60 if args.feed=='sip' else 300
    if args.poll_seconds<(60 if args.feed=='sip' else 300):raise ScanError('poll_interval_below_feed_minimum')
    key,secret=credentials();tag=hashlib.sha256(key.encode()).hexdigest()[:24]
    lock=Path(tempfile.gettempdir())/('trader-engine-market-scanner-'+tag+'.lock')
    stop=[False]
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:stop.__setitem__(0,True))
    with ExecutionLock(lock):
        scanner=Scanner(ReadOnlyTransport(key,secret,feed=args.feed),args.directory,futures=not args.no_futures,feed=args.feed)
        while not stop[0] and not (args.directory/'SHUTDOWN').exists():
            try:scanner.cycle()
            except Exception as exc:atomic(args.directory/'status.json',dict(checked_at=datetime.now(timezone.utc).isoformat(),status='degraded',mode='read_only',error=str(exc) if isinstance(exc,ScanError) else type(exc).__name__))
            if args.once:return 0
            end=time.monotonic()+args.poll_seconds
            while time.monotonic()<end and not stop[0] and not (args.directory/'SHUTDOWN').exists():time.sleep(min(1,max(0,end-time.monotonic())))
        atomic(args.directory/'status.json',dict(checked_at=datetime.now(timezone.utc).isoformat(),status='shutdown',mode='read_only'))
    return 0

if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        print(json.dumps(dict(status='stopped',error=str(exc) if isinstance(exc,ScanError) else type(exc).__name__)),flush=True);raise SystemExit(2)
