"""Read-only Alpaca paper-endpoint directory; never submits orders."""
from __future__ import annotations
import json
import os
import time
from datetime import date, datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, HTTPRedirectHandler, build_opener
from trader_engine.data.catalog import Instrument

BASE_URL='https://paper-api.alpaca.markets'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward credentials to another endpoint.


def request_directory(path, params):
    if path not in {'/v2/assets','/v2/options/contracts'}:
        raise ValueError('Only instrument-directory endpoints are allowed')
    key=os.environ.get('APCA_API_KEY_ID');secret=os.environ.get('APCA_API_SECRET_KEY')
    if not key or not secret:
        raise RuntimeError('Alpaca is not connected. Set paper API credentials locally in APCA_API_KEY_ID and APCA_API_SECRET_KEY.')
    request=Request(BASE_URL+path+'?'+urlencode(params),headers={'APCA-API-KEY-ID':key,'APCA-API-SECRET-KEY':secret},method='GET')
    try:
        with build_opener(NoRedirect).open(request,timeout=30) as response:
            raw=response.read(20_000_001)
        if len(raw)>20_000_000:raise ValueError('Alpaca directory page exceeds size limit')
        return json.loads(raw)
    except HTTPError as exc:
        raise RuntimeError(f'Alpaca directory HTTP {exc.code}; check paper credentials and entitlements') from None
    except URLError:
        raise RuntimeError('Alpaca directory connection failed') from None


def discover_alpaca(options_through: date | None=None, *, fetcher=request_directory, max_pages=1000, sleeper=time.sleep):
    now=datetime.now(timezone.utc);rows=[];coverage=[]
    if options_through is not None and options_through<now.date():
        raise ValueError('Options-through date must be today or later')
    if max_pages<1:raise ValueError('max_pages must be positive')
    for asset_class in ['us_equity','crypto']:
        payload=fetcher('/v2/assets',{'status':'active','asset_class':asset_class})
        if not isinstance(payload,list) or not payload:raise ValueError(f'Unexpected or empty Alpaca {asset_class} directory')
        for r in payload:
            kind='crypto' if asset_class=='crypto' else 'equity'
            # Alpaca us_equity includes ETFs; explicit naming helps classification,
            # while provider-native identity is preserved regardless of this display tag.
            if kind=='equity' and (' ETF' in r.get('name','').upper() or ' ETN' in r.get('name','').upper()):kind='etf'
            rows.append(Instrument(provider='alpaca_paper',venue=r.get('exchange') or 'Alpaca',symbol=r['symbol'],
                name=r.get('name') or r['symbol'],kind=kind,currency='USD' if asset_class=='us_equity' else r['symbol'].partition('/')[2],
                country='US' if asset_class=='us_equity' else '',status=r.get('status','unknown')+(' / broker-listed tradable' if r.get('tradable') else ' / broker-listed nontradable'),
                observed_at=now,source_url=BASE_URL+'/v2/assets'))
        coverage.append(dict(source='alpaca_'+asset_class,scope=f'Alpaca paper API active {asset_class} directory; account order access unverified',status='ok',instrument_count=len(payload)))
    if options_through is not None:
        token=None;seen=set();contracts=[]
        for page in range(max_pages):
            params={'status':'active','expiration_date_gte':now.date().isoformat(),
                'expiration_date_lte':options_through.isoformat(),'limit':1000}
            if token:params['page_token']=token
            payload=fetcher('/v2/options/contracts',params)
            if not isinstance(payload,dict) or not isinstance(payload.get('option_contracts'),list):raise ValueError('Unexpected Alpaca options response')
            for r in payload['option_contracts']:
                expiry=date.fromisoformat(r['expiration_date'])
                if not now.date()<=expiry<=options_through:raise ValueError('Option outside requested expiry range')
                contracts.append(Instrument(provider='alpaca_paper',venue='US listed options via Alpaca',symbol=r['symbol'],name=r.get('name') or r['symbol'],
                    kind='option',currency='USD',country='US',status=r.get('status','unknown'),expiry=r['expiration_date'],
                    strike=r['strike_price'],option_right=r['type'],contract_size=r['size'],underlying=r['underlying_symbol'],
                    observed_at=now,source_url=BASE_URL+'/v2/options/contracts'))
            token=payload.get('next_page_token')
            if not token:break
            if token in seen:raise ValueError('Repeated Alpaca pagination token; refusing incomplete directory')
            seen.add(token);sleeper(.35)
        else:raise ValueError('Alpaca options page limit reached; refusing to label truncated results complete')
        rows.extend(contracts)
        coverage.append(dict(source='alpaca_options',scope=f'Active options expiring {now.date()} through {options_through}; later/expired contracts excluded',status='ok',instrument_count=len(contracts)))
    else:
        coverage.append(dict(source='alpaca_options',scope='Not requested: choose an explicit expiry horizon',status='not_requested',instrument_count=0))
    return rows,coverage
