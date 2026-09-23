"""Read-only minute bars and session calendars; credentials only from environment."""
import os,json,re,time
from pathlib import Path
from urllib.request import Request,build_opener
from urllib.parse import urlencode
from urllib.error import HTTPError,URLError
import pandas as pd
from dataclasses import dataclass
from datetime import datetime, timezone
from trader_engine.data.alpaca_catalog import NoRedirect


READ_ONLY_ENDPOINTS = {
    'bars': 'https://data.alpaca.markets/v2/stocks/bars',
    'quotes': 'https://data.alpaca.markets/v2/stocks/quotes',
    'latest_quotes': 'https://data.alpaca.markets/v2/stocks/quotes/latest',
    'actions': 'https://data.alpaca.markets/v1/corporate-actions',
    'calendar': 'https://paper-api.alpaca.markets/v2/calendar',
}


class DataRequestError(RuntimeError):
    def __init__(self, kind, status_code=None):
        self.kind = kind
        self.status_code = status_code
        detail = f"HTTP {status_code}" if status_code is not None else "connection failure"
        super().__init__(f"Alpaca read-only {kind} request failed ({detail}); check credentials and feed access.")


@dataclass(frozen=True)
class DataResponse:
    payload: object
    raw: bytes
    received_at: str
    status: int = 200


def request_data_raw(kind, params):
    if kind not in READ_ONLY_ENDPOINTS:
        raise ValueError('Read-only market data/calendar endpoints only')
    key=os.environ.get('APCA_API_KEY_ID'); secret=os.environ.get('APCA_API_SECRET_KEY')
    if not key or not secret:
        raise RuntimeError('Set APCA_API_KEY_ID and APCA_API_SECRET_KEY in your local environment; do not put credentials in config files.')
    req=Request(READ_ONLY_ENDPOINTS[kind]+'?'+urlencode(params),
                headers={'APCA-API-KEY-ID':key,'APCA-API-SECRET-KEY':secret}, method='GET')
    for attempt in range(4):
        try:
            with build_opener(NoRedirect).open(req,timeout=30) as response:
                body=response.read(20000001)
                status=response.status
            if len(body)>20000000: raise ValueError('Oversized data response')
            return DataResponse(json.loads(body), body, datetime.now(timezone.utc).isoformat(), status)
        except HTTPError as exc:
            if exc.code==429 and attempt<3: time.sleep(2**attempt); continue
            raise DataRequestError(kind, exc.code) from None
        except URLError:
            raise DataRequestError(kind) from None


def request_data(kind, params):
    """Compatibility API returning decoded JSON; never changes broker state."""
    return request_data_raw(kind, params).payload


def download_minutes(symbols,start,end,output,*,feed='sip',fetcher=request_data,max_pages=1000):
    if not symbols or len(set(symbols))!=len(symbols) or any(not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}',s) for s in symbols):raise ValueError('Unique stock symbols required')
    if feed not in {'sip','iex'}:raise ValueError('Explicit SIP or IEX feed required')
    if pd.Timestamp(start)>pd.Timestamp(end):raise ValueError('Invalid date interval')
    raw_calendar=fetcher('calendar',{'start':start,'end':end})
    if not isinstance(raw_calendar,list) or not raw_calendar:raise ValueError('No exchange sessions returned')
    rows=[]
    for r in raw_calendar:
        if not start<=r['date']<=end:raise ValueError('Calendar outside requested range')
        rows.append({'open':pd.Timestamp(r['date']+' '+r['open'],tz='America/New_York').tz_convert('UTC'),'close':pd.Timestamp(r['date']+' '+r['close'],tz='America/New_York').tz_convert('UTC')})
    calendar=pd.DataFrame(rows);token=None;seen=set();data={s:[] for s in symbols}
    # Historical requests avoid the subscription-restricted most recent 15 minutes.
    request_end=min(pd.Timestamp(end+'T23:59:59Z'),pd.Timestamp.now(tz='UTC')-pd.Timedelta(minutes=16))
    for _ in range(max_pages):
        params=dict(symbols=','.join(symbols),timeframe='1Min',start=start+'T00:00:00Z',end=request_end.isoformat(),adjustment='raw',feed=feed,sort='asc',limit=10000,asof=end)
        if token:params['page_token']=token
        payload=fetcher('bars',params)
        if not isinstance(payload.get('bars'),dict):raise ValueError('Invalid bars response')
        for s,values in payload['bars'].items():
            if s not in data:raise ValueError('Unexpected symbol')
            data[s].extend(values)
        token=payload.get('next_page_token')
        if not token:break
        if token in seen:raise ValueError('Repeated page token')
        seen.add(token)
    else:raise ValueError('Page limit reached; incomplete download refused')
    frames={}
    for s,values in data.items():
        if not values:raise ValueError(f'No minute data for {s}')
        f=pd.DataFrame(values).rename(columns={'o':'open','h':'high','l':'low','c':'close','v':'volume'})
        f.index=pd.to_datetime(f.pop('t'),utc=True);frames[s]=f[['open','high','low','close','volume']].sort_index()
    from trader_engine.intraday.engine import validate_schedule,validate_minutes
    calendar=validate_schedule(calendar)
    for f in frames.values():validate_minutes(f)
    output=Path(output)
    if output.exists():raise FileExistsError('Choose a new directory; datasets are immutable')
    output.mkdir(parents=True)
    for s,f in frames.items():f.to_parquet(output/f'{s}.parquet')
    calendar.to_csv(output/'calendar.csv',index=False)
    import hashlib
    manifest={'provider':'alpaca','feed':feed,'adjustment':'raw','timeframe':'1Min','start':start,'end':end,'requested_end':request_end.isoformat(),'fetched_at':pd.Timestamp.now(tz='UTC').isoformat(),'symbols':symbols,'hashes':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir() if p.is_file()}}
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2))
    return manifest
