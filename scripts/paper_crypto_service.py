"""Persistent, paper-only BTC/ETH experiment. No live endpoint or blind retries."""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import Request, build_opener
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from trader_engine.execution.alpaca_paper import AlpacaPaperClient, BrokerError, _NoRedirect
from trader_engine.execution.journal import ExecutionLock
from trader_engine.execution.crypto_manager import plan_crypto_actions, SYMBOLS
D=Decimal
TERMINAL={'filled','canceled','expired','rejected'}
class ServiceError(ValueError):
    pass

FEE_ROUNDING_TOLERANCE=D('0.00000001') # Bounded ten 1e-9-unit fee-rounding steps; larger ambiguity fails closed.
SEED_ID='7264c339-aaa6-4e75-9e3c-82cb80bea1e4'
SEED_CLIENT='cx-20260924-sma200-btc-01'
SEED_QTY=D('0.146629544')

def symbol(value):
    return {'BTCUSD':'BTC/USD','ETHUSD':'ETH/USD'}.get(value,value)

def number(value):
    value=D(str(value))
    if not value.is_finite() or value<0:raise ServiceError('Invalid nonnegative number')
    return value

def atomic(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.tmp')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as handle:
        json.dump(value,handle,indent=2,allow_nan=False);handle.write('\n');handle.flush();os.fsync(handle.fileno())
    os.replace(temporary,path)
    fd=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)

def credentials():
    key=os.environ.get('APCA_API_KEY_ID');secret=os.environ.get('APCA_API_SECRET_KEY')
    if key and secret:return key,secret
    result=subprocess.run(['/usr/bin/security','find-generic-password','-s','codex.alpaca-mcp.paper','-a','alpaca-paper','-w'],capture_output=True,text=True,check=False)
    if result.returncode:raise ServiceError('Paper credential lookup failed')
    try:
        record=json.loads(result.stdout)
        return record['ALPACA_API_KEY'],record['ALPACA_SECRET_KEY']
    except (KeyError,ValueError,TypeError):raise ServiceError('Invalid paper credential record') from None

class CryptoPaperClient(AlpacaPaperClient):
    def submit(self,payload):
        if not self.allow_orders:raise BrokerError('Order mutations disabled')
        if payload.get('symbol') not in SYMBOLS or payload.get('side') not in ('buy','sell') or payload.get('time_in_force')!='ioc':raise ServiceError('Invalid crypto payload')
        if payload.get('type') not in ('limit','market') or number(payload.get('qty'))<=0:raise ServiceError('Invalid order')
        if payload['side']=='buy' and (payload.get('type')!='limit' or number(payload.get('limit_price'))<=0):raise ServiceError('Entry limit required')
        cid=payload.get('client_order_id','')
        if not cid.startswith('cm-') or len(cid)>48 or not all(c.isalnum() or c=='-' for c in cid):raise ServiceError('Invalid order identity')
        return self._object(self._request('POST','/v2/orders',payload=payload,entry=payload['side']=='buy'))
    def asset(self,s):
        return self._object(self._request('GET','/v2/assets/'+s.replace('/','')))

class Market:
    def __init__(self,key,secret):
        self.key=key;self.secret=secret;self.opener=build_opener(_NoRedirect())
    def get(self,path,params):
        if path not in ('bars','latest/quotes'):raise ServiceError('Invalid data path')
        request=Request('https://data.alpaca.markets/v1beta3/crypto/us/'+path+'?'+urlencode(params),headers={'APCA-API-KEY-ID':self.key,'APCA-API-SECRET-KEY':self.secret})
        try:
            with self.opener.open(request,timeout=15) as response:
                content=response.read(2*1024*1024+1)
                if not 200<=response.status<300 or len(content)>2*1024*1024:raise ServiceError('Market response invalid')
                return json.loads(content)
        except Exception:raise ServiceError('Market data unavailable') from None
    def snapshot(self,now):
        today=now.replace(hour=0,minute=0,second=0,microsecond=0)
        data=self.get('bars',dict(symbols=','.join(SYMBOLS),timeframe='1Day',start=(today-timedelta(days=200)).isoformat(),end=(today-timedelta(microseconds=1)).isoformat(),limit=1000,sort='asc'))
        if data.get('next_page_token'):raise ServiceError('Incomplete daily bar response')
        try:quotes=self.get('latest/quotes',dict(symbols=','.join(SYMBOLS)))
        except ServiceError:quotes={} # Quotes are entry evidence; missing quotes must not suppress daily exits.
        safe_quotes=quotes.get('quotes',{}) if isinstance(quotes,dict) else {}
        if not isinstance(safe_quotes,dict):safe_quotes={}
        return data.get('bars',{}),safe_quotes

def quantities(positions):
    result={}
    for p in positions:
        s=symbol(p['symbol'])
        if s in result:raise ServiceError('Duplicate position')
        result[s]=number(p['qty'])
    return result

class Service:
    def __init__(self,broker,market,directory,expected_account_id,*,observe_only=False,seed=None):
        self.broker=broker;self.market=market;self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True)
        self.expected=expected_account_id;self.observe=observe_only;self.seed=seed;self.storage_failed=False
        path=self.directory/'state.json'
        if path.exists():self.state=json.loads(path.read_text())
        else:self.state=dict(version=1,account_id=expected_account_id,ownership={},attempted={},pending=None,sequence=0,daily={},seeded=False)
        if self.state.get('account_id')!=self.expected:raise ServiceError('State account mismatch')
    def save(self):
        try:atomic(self.directory/'state.json',self.state)
        except Exception:
            self.storage_failed=True
            raise ServiceError('Durable state write failed; mutations disabled') from None
    def event(self,kind,**fields):
        with (self.directory/'journal.jsonl').open('a') as handle:
            handle.write(json.dumps(dict(at=datetime.now(timezone.utc).isoformat(),kind=kind,**fields),allow_nan=False)+'\n');handle.flush();os.fsync(handle.fileno())
    def seed_existing(self,account,positions):
        if self.state['seeded'] or not self.seed:return
        seed=json.loads(Path(self.seed).read_text())
        if seed.get('account_id')!=account['id'] or seed.get('client_order_id')!=SEED_CLIENT or seed.get('order_id')!=SEED_ID or number(seed.get('owned_net_qty'))!=SEED_QTY:raise ServiceError('Seed identity mismatch')
        order=self.broker.get_order(SEED_CLIENT)
        if not order or order.get('id')!=SEED_ID or order.get('status')!='filled' or order.get('side')!='buy' or symbol(order.get('symbol'))!='BTC/USD':raise ServiceError('Seed order not verified')
        filled=number(order['filled_qty'])
        if filled!=D('0.14699704') or not filled*D('.9975')-FEE_ROUNDING_TOLERANCE<=SEED_QTY<=filled:raise ServiceError('Seed net fee mismatch')
        if quantities(positions).get('BTC/USD')!=SEED_QTY:raise ServiceError('Seed position mismatch')
        attempts=[dict(client_order_id=SEED_CLIENT,symbol='BTC/USD',signal_day='2026-09-23')]+seed.get('prior_entry_attempts',[])
        for attempt in attempts:
            prior=self.broker.get_order(attempt['client_order_id'])
            if not prior or prior.get('side')!='buy' or symbol(prior.get('symbol'))!=attempt['symbol'] or prior.get('status') not in TERMINAL:
                raise ServiceError('Prior attempt cannot be verified')
            if attempt['client_order_id']!=SEED_CLIENT and number(prior.get('filled_qty','0'))!=0:
                raise ServiceError('Prior attempt has unowned fill')
            bucket=self.state['attempted'].setdefault(attempt['signal_day'],[])
            if attempt['symbol'] not in bucket:bucket.append(attempt['symbol'])
        self.state['ownership']['BTC/USD']=str(SEED_QTY);self.state['seeded']=True
        self.save();self.event('seed_verified',symbol='BTC/USD',quantity=str(SEED_QTY),order_id=SEED_ID)
    def reconcile(self,positions):
        intent=self.state['pending']
        if not intent:return True
        order=self.broker.get_order(intent['client_order_id'])
        if order is None:
            self.event('pending_not_found',client_order_id=intent['client_order_id']);return False
        payload=intent['payload'];s=payload['symbol']
        if order.get('client_order_id')!=intent['client_order_id'] or symbol(order.get('symbol'))!=s or order.get('side')!=payload['side']:raise ServiceError('Pending order identity mismatch')
        for field in ('type','time_in_force'):
            if order.get(field)!=payload[field]:raise ServiceError('Pending order terms mismatch')
        if number(order.get('qty'))!=number(payload['qty']):raise ServiceError('Pending order quantity mismatch')
        if payload['type']=='limit' and number(order.get('limit_price'))!=number(payload['limit_price']):raise ServiceError('Pending limit price mismatch')
        if order.get('status') not in TERMINAL:return False
        filled=number(order.get('filled_qty','0'));requested=number(payload['qty'])
        if filled>requested:raise ServiceError('Filled quantity exceeds intent')
        before=number(intent['position_before']);actual=quantities(positions).get(s,D(0))
        if payload['side']=='buy':
            delta=actual-before
            # Crypto buy fees are deducted in the purchased asset; require a bounded net delta.
            if (filled==0 and delta!=0) or not filled*D('.9975')-FEE_ROUNDING_TOLERANCE<=delta<=filled:raise ServiceError('Unreconciled buy fee or foreign position change')
            owned=number(intent['owned_before'])+delta
        else:
            if abs((before-actual)-filled)>D('.000000001'):raise ServiceError('Unreconciled sell or foreign position change')
            owned=number(intent['owned_before'])-filled
        if owned<0 or owned!=actual:raise ServiceError('Ownership does not equal broker holding')
        if owned:self.state['ownership'][s]=str(owned)
        else:self.state['ownership'].pop(s,None)
        self.state['pending']=None;self.save()
        self.event('terminal_reconciled',client_order_id=intent['client_order_id'],status=order['status'],filled_qty=str(filled),owned_qty=str(owned))
        return True
    def submit(self,plan,day,positions):
        if self.observe:raise ServiceError('Observation cannot submit')
        payload=dict(plan['order']);s=payload['symbol']
        if self.state['pending']:raise ServiceError('Pending intent blocks submission')
        if payload['side']=='buy':
            attempts=self.state['attempted'].setdefault(day,[])
            if s in attempts:raise ServiceError('Entry already attempted')
            attempts.append(s)
        self.state['sequence']+=1
        cid='cm-'+hashlib.sha256((self.expected+':'+str(self.directory.resolve())+':'+str(self.state['sequence'])).encode()).hexdigest()[:36]
        payload['client_order_id']=cid
        self.state['pending']=dict(client_order_id=cid,payload=payload,position_before=str(quantities(positions).get(s,D(0))),owned_before=self.state['ownership'].get(s,'0'),created_at=datetime.now(timezone.utc).isoformat())
        self.save() # Durable intent and attempted-day record precede the only POST.
        self.event('intent_created',client_order_id=cid,payload=payload)
        try:
            order=self.broker.submit(payload)
            self.event('submit_response',client_order_id=cid,status=order.get('status'))
        except Exception:
            self.event('submit_outcome_unknown',client_order_id=cid)
            return
    def tick(self,now=None):
        explicit_now=now is not None
        now=now or datetime.now(timezone.utc);checked=now.isoformat()
        status=dict(checked_at=checked,status='running',mode='observe_only' if self.observe else 'paper',error=None,decisions=[],signals={},pending=self.state['pending'])
        try:
            if self.storage_failed:raise ServiceError('Durable state write previously failed; restart reconciliation required')
            started=time.monotonic()
            account=self.broker.account()
            if account.get('id')!=self.expected:raise ServiceError('Broker account identity mismatch')
            positions=self.broker.positions();orders=self.broker.open_orders()
            status.update(account={k:account.get(k) for k in ('id','equity','cash','status','trading_blocked','account_blocked')},positions=positions)
            if not self.observe:self.seed_existing(account,positions)
            if not self.observe and not self.reconcile(positions):
                status['error']='pending_order_requires_reconciliation';return status
            if self.observe and self.state['pending']:
                status['error']='pending_order_requires_mutating_state_reconciliation';return status
            day=now.date().isoformat()
            if self.state['daily'].get('date')!=day:
                self.state['daily']=dict(date=day,start_equity=str(number(account['equity'])),entry_halted=False)
                if not self.observe:self.save()
            baseline=number(self.state['daily']['start_equity'])
            if number(account['equity'])<=baseline*D('.97'):
                self.state['daily']['entry_halted']=True
                if not self.observe:self.save()
            bars,quotes=self.market.snapshot(now)
            assets={}
            data_issues=[]
            for s in SYMBOLS:
                try:assets[s]=self.broker.asset(s)
                except Exception:data_issues.append(s+':asset_unavailable')
                if s not in quotes:data_issues.append(s+':quote_unavailable')
            status['data_issues']=data_issues
            if time.monotonic()-started>45:raise ServiceError('Snapshot collection exceeded freshness bound')
            snapshot=dict(now=checked if explicit_now else datetime.now(timezone.utc).isoformat(),expected_account_id=self.expected,account=account,positions=positions,open_orders=orders,bars=bars,quotes=quotes,assets=assets,
                          daily_start_equity=str(baseline),entries_enabled=not self.state['daily']['entry_halted'] and not (self.directory/'STOP').exists())
            result=plan_crypto_actions(snapshot,self.state['ownership'],self.state['attempted']);status['decisions']=result['plans'];status['signals']={'signal_day':result['signal_day']}
            exits=[p for p in result['plans'] if p['action']=='exit']
            entries=[p for p in result['plans'] if p['action']=='entry']
            owned_blocked=any(p['action']=='blocked' and number(self.state['ownership'].get(p['symbol'],'0'))>0 for p in result['plans'])
            if owned_blocked:
                status['status']='needs_attention';status['error']='owned_position_management_blocked'
            eligible=exits or ([] if orders or owned_blocked or self.state['daily']['entry_halted'] or (self.directory/'STOP').exists() else entries)
            if eligible and not self.observe:self.submit(eligible[0],result['signal_day'],positions)
            status['pending']=self.state['pending'];status['daily']=self.state['daily'];status['ownership']=self.state['ownership']
        except Exception as exc:
            status['status']='error';status['error']=str(exc) if isinstance(exc,ServiceError) else type(exc).__name__ # Never expose credentials, broker payloads or stack traces.
        finally:
            status['pending']=self.state['pending']
            atomic(self.directory/'status.json',status)
        return status

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',required=True,type=Path);parser.add_argument('--expected-account-id',required=True)
    parser.add_argument('--execute-paper',action='store_true');parser.add_argument('--seed',type=Path);parser.add_argument('--once',action='store_true');parser.add_argument('--observe-only',action='store_true');parser.add_argument('--poll-seconds',type=float,default=60.)
    args=parser.parse_args(argv)
    if args.execute_paper and args.observe_only:raise ServiceError('Choose execution or observation')
    args.observe_only=not args.execute_paper
    if args.poll_seconds<60:raise ServiceError('Polling cannot be faster than 60 seconds')
    key,secret=credentials();tag=hashlib.sha256(key.encode()).hexdigest()[:24]
    lock=Path(tempfile.gettempdir())/('trader-engine-paper-'+tag+'.lock')
    stopping=[False]
    for signum in (signal.SIGINT,signal.SIGTERM):signal.signal(signum,lambda *_:stopping.__setitem__(0,True))
    with ExecutionLock(lock),CryptoPaperClient(key,secret,allow_orders=not args.observe_only) as broker:
        market=Market(key,secret);service=Service(broker,market,args.directory,args.expected_account_id,observe_only=args.observe_only,seed=args.seed)
        while not stopping[0] and not (args.directory/'SHUTDOWN').exists():
            service.tick()
            if args.once:return 0
            until=time.monotonic()+args.poll_seconds
            while time.monotonic()<until and not stopping[0] and not (args.directory/'SHUTDOWN').exists():time.sleep(min(1,max(0,until-time.monotonic())))
        atomic(args.directory/'status.json',dict(checked_at=datetime.now(timezone.utc).isoformat(),status='shutdown',warning='Positions may remain held; shutdown does not liquidate.',ownership=service.state['ownership'],pending=service.state['pending']))
    return 0

if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'status':'stopped','error':type(exc).__name__}),flush=True);raise SystemExit(2)
