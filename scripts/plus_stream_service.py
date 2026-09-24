"""One persistent SIP data connection; no orders, fallbacks or raw event archive.

Requires websockets>=15,<17. Official protocol:
https://docs.alpaca.markets/us/docs/streaming-market-data
https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data
"""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import hashlib
import json
import logging
import re
from pathlib import Path
import shutil
import signal
import sys
import tempfile
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
sys.path.insert(0,str(ROOT/'scripts'))
from trader_engine.data.plus_stream import StreamState,StreamFailure,STREAM_URL,STREAM_URLS,QUOTE_SYMBOLS,MAX_FRAME_BYTES,decode_frame
from trader_engine.execution.journal import ExecutionLock
from market_scanner_service import credentials,atomic

def publish(state,directory,*,extra=None):
    directory=Path(directory)
    health,latest=state.snapshot(datetime.now(timezone.utc))
    if extra:health.update(extra)
    if shutil.disk_usage(directory).free<1024**3:
        health.update(status='degraded',error='disk_headroom_below_1GiB')
        atomic(directory/'status.json',health)
        raise StreamFailure('disk_headroom_below_1GiB')
    atomic(directory/'latest.json',latest)
    atomic(directory/'status.json',health)


def connect_stream(feed):
    from websockets.sync.client import connect
    # Sync connect performs one handshake, no redirect-following loop. Credentials
    # are sent only after this fixed-origin TLS connection has been established.
    quiet=logging.Logger('plus_stream_wire',level=logging.CRITICAL+1)
    return connect(STREAM_URLS[feed],additional_headers={'Content-Type':'application/msgpack' if feed=='opra' else 'application/json'},proxy=None,open_timeout=10,close_timeout=3,
                   ping_interval=20,ping_timeout=20,max_size=MAX_FRAME_BYTES,max_queue=16,
                   compression='deflate',logger=quiet)


def run_session(socket,state,key,secret,*,stop,flush,clock=time.monotonic,now=None,selection_refresh=None):
    """Injectable session owner for offline lifecycle tests. Never logs auth payloads."""
    now=now or (lambda:datetime.now(timezone.utc))
    state.begin_connection()
    def send(payload):
        if state.feed=='opra':
            import msgpack
            socket.send(msgpack.packb(payload,use_bin_type=True))
        else:socket.send(json.dumps(payload))
    send({'action':'auth','key':key,'secret':secret})
    deadline=clock()+10
    last_flush=clock()
    last_selection_check=clock()
    sent_subscription=False
    flush()
    while not stop():
        try:
            frame=socket.recv(timeout=1)
        except TimeoutError:
            frame=None
        if frame is not None:
            for message in decode_frame(frame,state.feed):
                event=state.ingest(message,now())
                if event=='authenticated' and not sent_subscription:
                    request={'action':'subscribe','quotes':list(state.quote_symbols)}
                    if state.feed=='sip':request['bars']=['*']
                    send(request)
                    sent_subscription=True
                    deadline=clock()+10
        if not state.subscribed and clock()>=deadline:
            raise StreamFailure('authentication_or_subscription_timeout')
        if selection_refresh is not None and clock()-last_selection_check>=900:
            selected=selection_refresh()
            last_selection_check=clock()
            if tuple(selected)!=state.quote_symbols:raise StreamFailure('option_selection_changed')
        if clock()-last_flush>=10:
            flush()
            last_flush=clock()
    flush()


def load_option_symbols(path,limit=100):
    if path is None:raise StreamFailure('options_file_required')
    if not 1<=limit<=1000:raise StreamFailure('option_quote_limit_invalid')
    try:
        payload=json.loads(Path(path).read_text())
        stamp=datetime.fromisoformat(payload['checked_at'].replace('Z','+00:00'))
        age=(datetime.now(timezone.utc)-stamp).total_seconds()
        if not 0<=age<=86400:raise ValueError('Old selection')
        contracts=payload['contracts']
        if not isinstance(contracts,list):raise ValueError('Invalid contracts')
        symbols=[]
        for row in contracts:
            symbol=row['symbol'] if isinstance(row,dict) else row
            if not isinstance(symbol,str) or not re.fullmatch(r'[A-Z][A-Z0-9.]{0,6}[0-9]{6}[CP][0-9]{8}',symbol):continue
            if datetime.strptime(symbol[-15:-9],'%y%m%d').date()<datetime.now(timezone.utc).date():continue
            if isinstance(row,dict) and (row.get('status','active')!='active' or row.get('tradable',True) is False):continue
            if symbol not in symbols:symbols.append(symbol)
            if len(symbols)==limit:break
        if not symbols:raise ValueError('Empty selection')
        return tuple(symbols)
    except Exception:raise StreamFailure('option_selection_unavailable_or_stale') from None


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',required=True,type=Path)
    parser.add_argument('--feed',choices=['sip','opra'],default='sip')
    parser.add_argument('--options-file',type=Path)
    parser.add_argument('--max-option-quotes',type=int,default=100)
    args=parser.parse_args(argv)
    args.directory.mkdir(parents=True,exist_ok=True)
    key,secret=credentials()
    tag=hashlib.sha256(key.encode()).hexdigest()[:24]
    lock=Path(tempfile.gettempdir())/('trader-engine-plus-'+args.feed+'-stream-'+tag+'.lock')
    stopping=[False]
    for sig in (signal.SIGINT,signal.SIGTERM):
        signal.signal(sig,lambda *_:stopping.__setitem__(0,True))
    def stop():return stopping[0] or (args.directory/'SHUTDOWN').exists()
    selection=load_option_symbols(args.options_file,args.max_option_quotes) if args.feed=='opra' else QUOTE_SYMBOLS
    state=StreamState(feed=args.feed,quote_symbols=selection)
    latest=args.directory/'latest.json'
    if latest.exists():
        try:state.restore(json.loads(latest.read_text()))
        except Exception:state.error='saved_observations_invalid'
    backoff=1
    with ExecutionLock(lock):
        while not stop():
            began=time.monotonic()
            try:
                publish(state,args.directory,extra={'status':'connecting','next_retry_seconds':0})
                if args.feed=='opra':
                    selected=load_option_symbols(args.options_file,args.max_option_quotes)
                    if tuple(selected)!=state.quote_symbols:
                        state.quote_symbols=tuple(selected)
                        state.quotes=type(state.quotes)((s,r) for s,r in state.quotes.items() if s in selected)
                with connect_stream(args.feed) as socket:
                    run_session(socket,state,key,secret,stop=stop,flush=lambda:publish(state,args.directory),selection_refresh=(lambda:load_option_symbols(args.options_file,args.max_option_quotes)) if args.feed=='opra' else None)
                if stop():break
                raise StreamFailure('connection_closed')
            except Exception as exc:
                reason=str(exc) if isinstance(exc,StreamFailure) else 'connection_failed'
                state.disconnected(reason)
                if reason=='disk_headroom_below_1GiB':return 2
                # Reset only after a sustained subscribed session, not after auth.
                if time.monotonic()-began>=60:backoff=1
                retry_at=time.monotonic()+backoff
                while not stop() and time.monotonic()<retry_at:
                    publish(state,args.directory,extra={'next_retry_seconds':max(0,round(retry_at-time.monotonic(),1))})
                    time.sleep(min(10,max(0,retry_at-time.monotonic())))
                backoff=min(60,backoff*2)
        state.connected=state.authenticated=state.subscribed=False
        publish(state,args.directory,extra={'status':'shutdown','next_retry_seconds':None})
    return 0

if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception:
        print(json.dumps({'status':'stopped','error':'stream_service_failed'}),flush=True)
        raise SystemExit(2)
