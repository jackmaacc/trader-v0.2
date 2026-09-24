"""Causal signal diagnostics. No order submission or strategy promotion."""
from datetime import datetime, timezone, timedelta
from math import isfinite

from trader_engine.execution.breakout import candidate

BASE_UNIVERSE = ('SPY', 'QQQ', 'IWM', 'DIA', 'TLT', 'GLD', 'XLK', 'XLF', 'XLE', 'XLV')


def time_value(value):
    stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('timestamp requires timezone')
    return stamp.astimezone(timezone.utc)


def select_universe(snapshot, now, limit=30):
    """Stable ETF core plus recent high-dollar-volume observed equities."""
    ranked = []
    for row in snapshot.get('records', []):
        try:
            if row['asset_class'] != 'us_equity' or row.get('retained') or row.get('feed') != 'sip' or row.get('currently_active') is not True or row.get('state') != 'fresh':
                continue
            if not 0 <= (now-time_value(row['data_asof'])).total_seconds() <= 300:
                continue
            if not 0 <= (now-time_value(row['volume_asof'])).total_seconds() <= 86400:
                continue
            price, volume = float(row['price']), float(row['volume'])
            if not isfinite(price*volume) or price < 5 or volume <= 0:
                continue
            ranked.append((price*volume, row['symbol']))
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
    names = list(BASE_UNIVERSE)
    for _, symbol in sorted(ranked, reverse=True):
        if symbol not in names:
            names.append(symbol)
        if len(names) >= limit:
            break
    return names[:limit]



def valid_bar_window(bars, now):
    try:
        completed = sorted((time_value(b['t']), b) for b in bars if time_value(b['t'])+timedelta(minutes=1) <= now)
        window = completed[-16:]
        if len(window) != 16 or not 60 <= (now-window[-1][0]).total_seconds() <= 120:
            return False
        if any((window[i][0]-window[i-1][0]).total_seconds() != 60 for i in range(1,16)):
            return False
        for _, row in window:
            o,h,l,c,v = (float(row[k]) for k in ('o','h','l','c','v'))
            if not all(isfinite(x) for x in (o,h,l,c,v)) or not (0 < l <= min(o,c) <= max(o,c) <= h and v > 0):
                return False
        return True
    except (KeyError, TypeError, ValueError, OverflowError):
        return False

def evaluate(symbols, bars_result, quotes_result, now):
    grouped = {s: [] for s in symbols}
    for row in bars_result.get('records', []):
        if row.get('symbol') in grouped:
            grouped[row['symbol']].append(row)
    quotes = {r.get('symbol'): r for r in quotes_result.get('records', [])}
    output = []
    complete = bars_result.get('complete') is True and quotes_result.get('complete') is True
    correct_feed = all(r.get('provenance', {}).get('feed') == 'sip' for r in (bars_result, quotes_result))
    for symbol in symbols:
        reasons = []
        signal = None
        if not complete:
            reasons.append('incomplete_source_response')
        if not correct_feed:
            reasons.append('consolidated_feed_not_verified')
        if complete and correct_feed:
            signal = candidate(grouped[symbol], now)
        if not valid_bar_window(grouped[symbol], now):
            reasons.append('invalid_incomplete_or_stale_bar_window')
        if signal is None:
            reasons.append('no_valid_recent_completed_breakout')
        quote = quotes.get(symbol, {})
        spread = None
        try:
            age = (now-time_value(quote['t'])).total_seconds()
            bid, ask = float(quote['bp']), float(quote['ap'])
            if not all(isfinite(v) for v in (bid, ask)) or not 0 < bid <= ask:
                raise ValueError('invalid quote')
            if not 0 <= age <= 10:
                raise ValueError('stale quote')
            spread = (ask-bid)/((ask+bid)/2)*10000
            if spread > 25:
                reasons.append('spread_above_25bps')
        except (KeyError, TypeError, ValueError, OverflowError):
            reasons.append('missing_invalid_or_stale_quote')
        # The legacy hypothesis is observed unchanged, not reinstated as qualified.
        reasons.append('strategy_not_approved_for_broker_execution')
        output.append(dict(symbol=symbol, evaluated_at=now.isoformat(),
                           strategy='existing_breakout_diagnostic', signal=signal,
                           spread_bps=spread, quote_asof=quote.get('t'),
                           decision='data_blocked' if any(r in reasons for r in ('incomplete_source_response', 'consolidated_feed_not_verified', 'missing_invalid_or_stale_quote', 'invalid_incomplete_or_stale_bar_window')) else 'blocked' if signal else 'no_signal', reasons=reasons,
                           broker_execution_enabled=False))
    return output


def merge_stream_quotes(rest_result, stream, now):
    """Use newer validated SIP stream quotes; retain explicit per-row provenance."""
    result = dict(rest_result)
    records = {r['symbol']: dict(r, source_transport='rest') for r in rest_result.get('records', []) if isinstance(r, dict) and r.get('symbol')}
    try:
        if stream.get('feed') != 'sip' or not 0 <= (now-time_value(stream['checked_at'])).total_seconds() <= 30:
            return result
        for symbol, quote in stream.get('quotes', {}).items():
            if quote.get('state') != 'fresh' or quote.get('source') != 'alpaca_sip' or quote.get('symbol') != symbol:
                continue
            if not 0 <= (now-time_value(quote['received_at'])).total_seconds() <= 10:
                continue
            stamp = time_value(quote['data_asof'])
            if not 0 <= (now-stamp).total_seconds() <= 10 or symbol not in records:
                continue
            old_stamp = time_value(records[symbol]['t'])
            if stamp > old_stamp:
                records[symbol] = dict(symbol=symbol,t=quote['data_asof'],bp=quote['bid'],ap=quote['ask'],bs=quote['bid_size'],**{'as':quote['ask_size']},source_transport='sip_websocket')
    except (KeyError, ValueError, TypeError):
        return result
    result['records'] = list(records.values())
    result['stream_checked_at'] = stream.get('checked_at')
    return result
