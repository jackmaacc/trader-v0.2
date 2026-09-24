"""Pure descriptive observations. This module generates no orders or forecasts."""
from datetime import datetime, timezone
import math

FUTURES=('ES=F','NQ=F','YM=F','RTY=F','GC=F','SI=F','CL=F','ZN=F')

def timestamp(value):
    if isinstance(value,(int,float)):
        result=datetime.fromtimestamp(value,timezone.utc)
    else:
        result=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if result.tzinfo is None:raise ValueError('Timestamp must include timezone')
    return result.astimezone(timezone.utc)

def finite(value,positive=False):
    if isinstance(value,bool):raise ValueError('Boolean is not a price')
    result=float(value)
    if not math.isfinite(result) or result<0 or (positive and result<=0):raise ValueError('Invalid numeric observation')
    return result

def observe(symbol,asset_class,payload,now,*,market_open=None,previous=None,failure=None,feed="iex"):
    """One symbol, preserving explicit last observation on missing/error responses."""
    now=timestamp(now);previous=previous or {}
    if feed not in ('iex','sip'):raise ValueError('Explicit stock feed required')
    source={'us_equity':'alpaca_'+feed,'crypto':'alpaca_crypto_us','futures':'indicative_continuous_futures_proxy'}[asset_class]
    record={k:previous.get(k) for k in ('price','data_asof','change_pct','volume','spread_bps','last_data_asof','comparison_asof','volume_asof')}
    record.update(symbol=symbol,asset_class=asset_class,source=source,state='missing',reason=None,
                  first_seen=previous.get('first_seen',now.isoformat()),last_seen=previous.get('last_seen'),
                  checked_at=now.isoformat(),observation_count=int(previous.get('observation_count',0)),
                  scan_count=int(previous.get('scan_count',0))+1,execution_eligible=False,
                  retained=bool(previous.get('data_asof')),feed=feed if asset_class=='us_equity' else None,requested_source=source,requested_feed=feed if asset_class=='us_equity' else None)
    if record['retained']:
        record['source']=previous.get('source',source);record['feed']=previous.get('feed')
    if failure:
        record.update(state='unavailable',reason=failure);return record
    if payload is None:
        record['reason']='symbol_missing_from_response';return record
    try:
        if not isinstance(payload,dict):raise ValueError('Invalid snapshot shape')
        trade=payload.get('latestTrade') or payload.get('latest_trade') or {}
        daily=payload.get('dailyBar') or payload.get('daily_bar') or {}
        prior=payload.get('prevDailyBar') or payload.get('prev_daily_bar') or {}
        quote=payload.get('latestQuote') or payload.get('latest_quote') or {}
        if not all(isinstance(x,dict) for x in (trade,daily,prior,quote)):raise ValueError('Invalid bar or quote shape')
        selected=trade if trade else daily
        price=finite(selected.get('p',selected.get('c')),True)
        asof=timestamp(selected['t']);age=(now-asof).total_seconds()
        if age<0:raise ValueError('Future-dated price observation')
        volume=finite(daily['v']) if 'v' in daily else None
        volume_asof=None
        if volume is not None:
            try:
                stamp=timestamp(daily['t'])
                if not 0<=(now-stamp).total_seconds()<=7*86400:raise ValueError('Volume interval invalid')
                volume_asof=stamp.isoformat()
            except (KeyError,ValueError,TypeError,OverflowError):volume=None
        previous_close=None;comparison_asof=None
        if 'c' in prior:
            try:
                stamp=timestamp(prior['t'])
                if not stamp<asof or (asof-stamp).total_seconds()>7*86400:raise ValueError('Comparison interval invalid')
                previous_close=finite(prior['c'],True);comparison_asof=stamp.isoformat()
            except (KeyError,ValueError,TypeError,OverflowError):pass
        spread=None
        if quote:
            ask,bid=finite(quote['ap'],True),finite(quote['bp'],True)
            if ask<bid:raise ValueError('Crossed quote')
            quote_age=(now-timestamp(quote['t'])).total_seconds()
            if quote_age<0:raise ValueError('Future-dated quote')
            # Old quotes cannot describe current spread even if trade is fresh.
            if quote_age<=(180 if asset_class=='crypto' else 300):spread=(ask-bid)/((ask+bid)/2)*10000
        if asset_class=='futures':state='indicative_unknown_latency' if age<=1800 else 'stale'
        elif asset_class=='us_equity' and market_open is False:state='closed_last_observation' if age<=7*86400 else 'stale'
        elif asset_class=='us_equity' and market_open is None:state='unavailable'
        else:state='fresh' if age<=(180 if asset_class=='crypto' else 300) else 'stale'
        change=(price/previous_close-1)*100 if previous_close else None
        if change is not None and not math.isfinite(change):change=None
        if spread is not None and not math.isfinite(spread):spread=None
        new_identity=(asof.isoformat(),price,volume)
        old_identity=(previous.get('data_asof'),previous.get('price'),previous.get('volume'))
        record.update(price=price,source=source,feed=feed if asset_class=='us_equity' else None,data_asof=asof.isoformat(),change_pct=change,
                      volume=volume,volume_asof=volume_asof,comparison_asof=comparison_asof,spread_bps=spread,state=state,retained=False,last_seen=now.isoformat(),
                      last_data_asof=asof.isoformat(),age_seconds=age,
                      observation_count=record['observation_count']+int(new_identity!=old_identity))
        if state=='unavailable':record['reason']='market_clock_unavailable'
        elif state=='indicative_unknown_latency':record['reason']='continuous_proxy_with_unknown_delay_not_an_executable_contract'
        return record
    except (KeyError,TypeError,ValueError,OverflowError,OSError):
        record.update(state='invalid',reason='invalid_price_timestamp_bar_or_quote');return record

class FuturesWindowEmpty(ValueError):
    """A valid chart window contains no priced observations; a wider query may help."""


def futures_snapshot(payload):
    """Yahoo chart to descriptive snapshot; no contract metadata is inferred."""
    chart=payload.get('chart') if isinstance(payload,dict) else None
    if isinstance(chart,dict) and chart.get('error') is not None:raise ValueError('Provider reported futures error')
    results=chart.get('result') if isinstance(chart,dict) else None
    if not isinstance(results,list) or len(results)!=1:raise ValueError('Unavailable futures chart')
    result=results[0]
    if not isinstance(result,dict):raise ValueError('Malformed futures result')
    times=result.get('timestamp',[])
    indicators=result.get('indicators')
    if not isinstance(times,list) or not isinstance(indicators,dict):raise ValueError('Malformed futures observations')
    quotes=indicators.get('quote',[])
    if not isinstance(quotes,list) or not quotes or not isinstance(quotes[0],dict):raise ValueError('Malformed futures bars')
    closes=quotes[0].get('close',[])
    if not isinstance(closes,list) or len(times)!=len(closes):raise ValueError('Mismatched futures bars')
    stamps=[]
    for value in times:
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):raise ValueError('Invalid futures timestamp')
        stamps.append(timestamp(value))
    if any(current<=previous for previous,current in zip(stamps,stamps[1:])):raise ValueError('Unordered futures timestamps')
    candidates=[i for i,c in enumerate(closes) if c is not None]
    if not candidates:raise FuturesWindowEmpty('futures_window_has_no_priced_bars')
    i=candidates[-1];price=finite(closes[i],True);stamp=stamps[i].isoformat()
    output={'latestTrade':{'p':price,'t':stamp}}
    metadata=result.get('meta',{})
    if not isinstance(metadata,dict):raise ValueError('Malformed futures metadata')
    previous=metadata.get('chartPreviousClose')
    if previous is not None:output['prevDailyBar']={'c':finite(previous,True)}
    # Latest minute volume is not mislabeled as daily volume.
    return output

def summarize(records):
    counts={}
    for row in records:counts[row['state']]=counts.get(row['state'],0)+1
    movers=sorted((r for r in records if r['state']=='fresh' and r.get('change_pct') is not None),key=lambda r:(-abs(r['change_pct']),r['symbol']))[:50]
    return {'total':len(records),'by_state':counts},movers
