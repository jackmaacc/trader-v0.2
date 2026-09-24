"""Bounded, descriptive SIP stream state. No order or account capabilities."""
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
import json
import math

STREAM_URL = 'wss://stream.data.alpaca.markets/v2/sip'
STREAM_URLS = {'sip':STREAM_URL,'opra':'wss://stream.data.alpaca.markets/v1beta1/opra'}
QUOTE_SYMBOLS = ('SPY','QQQ','IWM','DIA','VTI','VOO','IVV','TLT','IEF','SHY',
                 'GLD','SLV','XLF','XLK','XLE','XLV','XLI','XLP','XLY','XLU')
MAX_FRAME_BYTES = 4 * 1024 * 1024
ERRORS = {400:'invalid_request',401:'not_authenticated',402:'authentication_failed',
          403:'already_authenticated',404:'authentication_timeout',405:'subscription_limit',
          406:'connection_limit',407:'slow_client',409:'insufficient_subscription',
          410:'unsupported_channel',500:'provider_internal_error'}

class StreamFailure(RuntimeError):
    """Controlled message safe to persist; never include a server's raw text."""

def utc(value):
    result = value if isinstance(value,datetime) else datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if result.tzinfo is None:
        raise ValueError('Timezone required')
    return result.astimezone(timezone.utc)

def number(value, *, positive=False):
    if isinstance(value,bool):
        raise ValueError('Boolean numeric field')
    result=float(value)
    if not math.isfinite(result) or result<0 or (positive and result<=0):
        raise ValueError('Invalid numeric field')
    return result

def decode_frame(raw,feed='sip'):
    if not isinstance(raw,(str,bytes)) or len(raw)>MAX_FRAME_BYTES:
        raise StreamFailure('invalid_or_oversized_frame')
    try:
        if feed=='opra':
            import msgpack
            data=msgpack.unpackb(raw,raw=False,timestamp=3,max_array_len=50000,max_map_len=100,max_str_len=1024,max_bin_len=MAX_FRAME_BYTES)
        else:data=json.loads(raw)
    except (ValueError,UnicodeError,TypeError):
        raise StreamFailure('invalid_json_frame') from None
    if not isinstance(data,list) or len(data)>50000 or any(not isinstance(row,dict) for row in data):
        raise StreamFailure('invalid_frame_shape')
    return data

class StreamState:
    def __init__(self, *, max_bar_symbols=25000,feed='sip',quote_symbols=None):
        if not 1<=max_bar_symbols<=25000:
            raise ValueError('Invalid bar memory bound')
        if feed not in STREAM_URLS:raise ValueError('Unsupported stream feed')
        self.feed=feed
        self.quote_symbols=tuple(quote_symbols if quote_symbols is not None else QUOTE_SYMBOLS)
        if not self.quote_symbols or len(self.quote_symbols)>(1000 if feed=='opra' else 20) or len(set(self.quote_symbols))!=len(self.quote_symbols) or any(not isinstance(s,str) or not s or '*' in s or len(s)>64 for s in self.quote_symbols):raise ValueError('Invalid quote selection')
        self.max_bar_symbols=max_bar_symbols
        self.bars=OrderedDict()
        self.quotes=OrderedDict()
        self.generation=0
        self.connected=False
        self.authenticated=False
        self.subscribed=False
        self.last_message_at=None
        self.last_data_at=None
        self.error=None
        self.reconnect_count=0
        self.counts=dict(bars=0,quotes=0,invalid=0,out_of_order=0,evictions=0,restored=0)

    def begin_connection(self):
        self.generation+=1
        self.connected=True
        self.authenticated=False
        self.subscribed=False
        self.error=None
        self.last_message_at=None

    def disconnected(self,reason):
        self.connected=False
        self.authenticated=False
        self.subscribed=False
        self.error=reason
        self.reconnect_count+=1

    def ingest(self,row,now):
        """Return authenticated/subscribed control events, or None for observations."""
        now=utc(now)
        self.last_message_at=now.isoformat()
        kind=row.get('T')
        if kind=='error':
            code=row.get('code')
            reason=ERRORS.get(code,'provider_error') if isinstance(code,int) else 'provider_error'
            self.error=reason
            raise StreamFailure(reason)
        if kind=='success':
            if row.get('msg')=='authenticated':
                self.authenticated=True
                return 'authenticated'
            return None
        if kind=='subscription':
            bars,quotes=row.get('bars'),row.get('quotes')
            if not self.authenticated or (self.feed=='sip' and not isinstance(bars,list)) or not isinstance(quotes,list) or (bars!=['*'] if self.feed=='sip' else bars not in (None,[])) or set(quotes)!=set(self.quote_symbols):
                raise StreamFailure('subscription_not_confirmed')
            self.subscribed=True
            return 'subscribed'
        if kind not in ('b','q'):
            return None
        try:
            if not self.authenticated or not self.subscribed:
                raise ValueError('Unconfirmed stream')
            symbol=row['S']
            if not isinstance(symbol,str) or not symbol or len(symbol)>64:
                raise ValueError('Invalid symbol')
            stamp=utc(row['t'])
            if stamp>now:
                raise ValueError('Future data')
            item=dict(symbol=symbol,data_asof=stamp.isoformat(),received_at=now.isoformat(),source='alpaca_'+self.feed,generation=self.generation)
            if kind=='b':
                if self.feed!='sip':raise ValueError('Unexpected OPRA bar')
                if stamp+timedelta(minutes=1)>now:
                    raise ValueError('Incomplete minute bar')
                item.update(open=number(row['o'],positive=True),high=number(row['h'],positive=True),low=number(row['l'],positive=True),close=number(row['c'],positive=True),volume=number(row['v']))
                if item['high']<max(item['open'],item['close'],item['low']) or item['low']>min(item['open'],item['close'],item['high']):
                    raise ValueError('Inconsistent bar')
                target=self.bars
            else:
                if symbol not in self.quote_symbols:
                    raise ValueError('Unrequested quote')
                item.update(bid=number(row['bp'],positive=self.feed=='sip'),ask=number(row['ap'],positive=True),bid_size=number(row['bs']),ask_size=number(row['as']))
                if item['ask']<item['bid']:
                    raise ValueError('Crossed quote')
                target=self.quotes
            previous=target.get(symbol)
            if previous and stamp<utc(previous['data_asof']):
                self.counts['out_of_order']+=1
                return None
            target[symbol]=item
            target.move_to_end(symbol)
            if kind=='b' and len(target)>self.max_bar_symbols:
                target.popitem(last=False)
                self.counts['evictions']+=1
            self.counts['bars' if kind=='b' else 'quotes']+=1
            self.last_data_at=now.isoformat()
        except (KeyError,TypeError,ValueError,OverflowError):
            self.counts['invalid']+=1
        return None

    def restore(self,latest):
        """Retain bounded old observations, never restore authentication or freshness."""
        if not isinstance(latest,dict) or latest.get('feed')!=self.feed:
            return
        original_counts=dict(self.counts)
        for channel,kind in (('bars','b'),('quotes','q')):
            rows=latest.get(channel,{})
            if not isinstance(rows,dict):
                continue
            limit=self.max_bar_symbols if kind=='b' else len(self.quote_symbols)
            for symbol,item in list(rows.items())[-limit:]:
                if not isinstance(item,dict):
                    continue
                try:
                    stamp=utc(item['data_asof'])
                    received=utc(item['received_at'])
                    row={'T':kind,'S':symbol,'t':stamp.isoformat()}
                    mapping={'o':'open','h':'high','l':'low','c':'close','v':'volume'} if kind=='b' else {'bp':'bid','ap':'ask','bs':'bid_size','as':'ask_size'}
                    row.update({short:item[long] for short,long in mapping.items()})
                    self.authenticated=self.subscribed=True
                    self.ingest(row,max(received,stamp+timedelta(minutes=1) if kind=='b' else stamp))
                except (KeyError,TypeError,ValueError,OverflowError):
                    continue
                finally:
                    self.authenticated=self.subscribed=False
        self.last_message_at=None
        self.last_data_at=None
        self.counts=original_counts
        self.counts['restored']=len(self.bars)+len(self.quotes)

    def snapshot(self,now):
        now=utc(now)
        fresh=dict(bars=0,quotes=0)
        output={}
        for channel,values in (('bars',self.bars),('quotes',self.quotes)):
            records={}
            for symbol,record in values.items():
                value=dict(record)
                age=(now-utc(value['data_asof'])).total_seconds()
                current=value['generation']==self.generation and self.connected and self.authenticated and self.subscribed
                state='fresh' if current and 0<=age<=(180 if channel=='bars' else 60) else ('stale' if current else 'retained_previous_session')
                value.update(state=state,age_seconds=age)
                fresh[channel]+=int(state=='fresh')
                records[symbol]=value
            output[channel]=records
        data_state='fresh' if any(fresh.values()) else ('stale' if self.bars or self.quotes else 'awaiting_data')
        status='streaming' if self.subscribed and data_state=='fresh' else ('subscribed_idle' if self.subscribed else ('authenticating' if self.connected else 'disconnected'))
        if self.error:status='degraded'
        common=dict(checked_at=now.isoformat(),feed=self.feed,mode='read_only')
        health=dict(**common,status=status,connected=self.connected,authenticated=self.authenticated,subscribed=self.subscribed,
                    subscription={'bars':['*'] if self.feed=='sip' else [],'quotes':list(self.quote_symbols)},last_message_at=self.last_message_at,last_data_at=self.last_data_at,
                    reconnect_count=self.reconnect_count,error=self.error,counts=dict(self.counts),fresh_counts=fresh,data_state=data_state,
                    complete_market_coverage=False,coverage_note='Latest observed symbols only; no message does not prove no market activity.')
        latest=dict(**common,**output,complete_market_coverage=False)
        return health,latest
