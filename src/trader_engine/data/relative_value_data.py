"""Immutable adjusted-price daily research dataset; read-only Alpaca endpoints."""
from pathlib import Path
import json,hashlib,re
import pandas as pd
from trader_engine.data.alpaca_intraday import request_data
from trader_engine.data.base import validate_bars


def download_relative_data(symbols,start,end,output,*,fetcher=request_data,max_pages=1000):
    output=Path(output)
    if output.exists():raise FileExistsError('Use a new data directory')
    if not symbols or len(symbols)!=len(set(symbols)) or any(not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}',s) for s in symbols):raise ValueError('Invalid symbols')
    if pd.Timestamp(start)>pd.Timestamp(end):raise ValueError('Invalid date range')
    calendar=fetcher('calendar',dict(start=start,end=end))
    if not isinstance(calendar,list) or not calendar:raise ValueError('No exchange calendar')
    rows=[]
    cutoff=pd.Timestamp.now(tz='UTC')-pd.Timedelta(minutes=16)
    for r in calendar:
        if not start<=r['date']<=end:raise ValueError('Calendar out of range')
        close=pd.Timestamp(r['date']+' '+r['close'],tz='America/New_York')
        if close>cutoff:raise ValueError('Requested interval contains an incomplete session')
        rows.append(r['date'])
    dates=pd.DatetimeIndex(pd.to_datetime(rows))
    if dates.has_duplicates or not dates.is_monotonic_increasing:raise ValueError('Invalid calendar')
    data={s:[] for s in symbols};token=None;seen=set()
    for _ in range(max_pages):
        p=dict(symbols=','.join(symbols),timeframe='1Day',start=start+'T00:00:00Z',end=min(pd.Timestamp(end+'T23:59:59Z'),cutoff).isoformat(),feed='sip',adjustment='all',asof=end,sort='asc',limit=10000)
        if token:p['page_token']=token
        payload=fetcher('bars',p)
        if not isinstance(payload.get('bars'),dict):raise ValueError('Invalid bars response')
        for s,values in payload['bars'].items():
            if s not in data:raise ValueError('Unexpected symbol')
            data[s].extend(values)
        token=payload.get('next_page_token')
        if not token:break
        if token in seen:raise ValueError('Repeated pagination token')
        seen.add(token)
    else:raise ValueError('Incomplete pagination')
    frames={}
    for s,values in data.items():
        if not values:raise ValueError('No data for '+s)
        f=pd.DataFrame(values).rename(columns=dict(o='open',h='high',l='low',c='close',v='volume'))
        f.index=pd.to_datetime(f.pop('t'),utc=True).dt.tz_convert('America/New_York').dt.tz_localize(None).dt.normalize();f=f[['open','high','low','close','volume']].sort_index()
        validate_bars(f)
        if not f.index.equals(dates):raise ValueError('Missing or unexpected daily sessions: '+s)
        frames[s]=f
    output.mkdir(parents=True)
    for s,f in frames.items():f.to_parquet(output/f'{s}.parquet')
    pd.DataFrame({'session':dates}).to_csv(output/'calendar.csv',index=False)
    manifest=dict(provider='alpaca',feed='sip',timeframe='1Day',adjustment='all',price_interpretation='non-executable adjusted total-return proxy',start=start,end=end,symbols=symbols,created_at=pd.Timestamp.now(tz='UTC').isoformat(),hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir() if p.is_file()})
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2));return manifest
