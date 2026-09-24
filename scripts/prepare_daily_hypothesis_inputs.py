"""Build offline daily inputs; provisional corrections require an explicit option."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
import numpy as np
import pandas as pd
from import_connected_alpaca import read, bars
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from trader_engine.data.evidence import parse_calendar

SYMBOLS = ('SPY', 'QQQ', 'IWM', 'TLT', 'GLD')

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

QQQ_PROVISIONAL_REMOVAL = 'd0409c14-2b32-4cdb-9d5d-57a8a9a0818f'
QQQ_RETAINED = '1966d92d-c3ae-4da1-8daa-483789298533'

def provisional_actions(actions, enabled=False):
    """Preserve ambiguity unless the caller explicitly selects the recorded repair."""
    if not enabled:
        return actions.copy()
    for identity, payment in ((QQQ_PROVISIONAL_REMOVAL, '2022-09-23'), (QQQ_RETAINED, '2022-10-31')):
        matches = actions.loc[actions.source_id == identity]
        if len(matches) != 1:
            raise ValueError('Provisional correction expected both exact source identities')
        row = matches.iloc[0]
        if row.symbol != 'QQQ' or row.name.tz_convert('America/New_York').date().isoformat() != '2022-09-19' or abs(row.cash_dividend - .51856) > 1e-12 or row.payment_timestamp.date().isoformat() != payment:
            raise ValueError('Provisional correction source content changed')
    return actions.loc[actions.source_id != QQQ_PROVISIONAL_REMOVAL].copy()

def causal_total_return(frame, actions, symbol):
    """Forward index; historical publication/arrival provenance is still unknown."""
    events = actions.loc[actions.symbol == symbol].copy()
    days = events.index.tz_convert('America/New_York').normalize()
    if days.has_duplicates:
        raise ValueError('Ambiguous same-session corporate actions')
    events.index = days
    result = frame.close.copy()
    for i in range(1, len(frame)):
        day = frame.index[i]
        ratio, dividend = 1., 0.
        if day in events.index:
            event = events.loc[day]
            ratio, dividend = float(event.split_ratio), float(event.cash_dividend)
        result.iloc[i] = result.iloc[i-1] * ratio * (frame.close.iloc[i] + dividend) / frame.close.iloc[i-1]
    return result

def prepare(source, refresh, output, *, provisional_qqq_correction=False):
    source, refresh, output = map(Path, (source, refresh, output))
    manifest = json.loads((source / 'dataset/manifest.json').read_text())
    refresh_manifest = json.loads((refresh / 'dataset/manifest.json').read_text())
    source_hashes = {}
    def checked(path, expected):
        actual = sha(path)
        if actual != expected:
            raise ValueError('Archive integrity failure: ' + str(path))
        source_hashes[str(path.resolve())] = actual
    checked(source / 'raw/calendar.json.gz', manifest['source_hashes']['calendar.json.gz'])
    checked(source / 'raw/actions.json.gz', manifest['source_hashes']['actions.json.gz'])
    checked(refresh / 'dataset/actions.parquet', refresh_manifest['hashes']['actions.parquet'])
    calendar = parse_calendar(read(source / 'raw/calendar.json.gz')['result'])
    dates = [r['date'] for r in calendar]
    schedule = pd.DataFrame({'market_open': pd.to_datetime([r['open'] for r in calendar], utc=True),
                             'market_close': pd.to_datetime([r['close'] for r in calendar], utc=True)})
    schedule['previous_session'] = pd.Series(dates).shift()
    opening = dict(zip(dates, schedule.market_open))
    frames = {}
    for symbol in SYMBOLS:
        loaded = {}
        for adjustment in ('raw', 'all'):
            key = f'{symbol}/daily_{adjustment}.json.gz'
            path = source / 'raw' / key
            checked(path, manifest['source_hashes'][key])
            payload = read(path)
            if payload.get('next_page_token'):
                raise ValueError('Incomplete daily pagination')
            frame = bars(payload, symbol)
            frame.index = frame.index.tz_convert('America/New_York').normalize()
            if frame.index.has_duplicates or list(frame.index.strftime('%Y-%m-%d')) != dates:
                raise ValueError('Daily calendar mismatch: ' + symbol)
            if not np.isfinite(frame.to_numpy()).all() or (frame[['open','high','low','close']] <= 0).any().any() or (frame.volume < 0).any():
                raise ValueError('Invalid daily values')
            if (frame.high < frame[['open','close','low']].max(axis=1)).any() or (frame.low > frame[['open','close','high']].min(axis=1)).any():
                raise ValueError('Invalid daily OHLC')
            loaded[adjustment] = frame
        frame = loaded['raw'].copy()
        frame['total_return_close'] = loaded['all'].close
        for column in ('high', 'low', 'close'):
            frame['signal_' + column] = loaded['all'][column]
        frame['signal_scale'] = frame.close / loaded['all'].close
        frames[symbol] = frame
    payload = read(source / 'raw/actions.json.gz')
    if payload.get('next_page_token') or set(payload['corporate_actions']) - {'cash_dividends'}:
        raise ValueError('Incomplete or unsupported corporate action archive')
    records = []
    for row in payload['corporate_actions']['cash_dividends']:
        if row['symbol'] not in SYMBOLS or row['ex_date'] not in opening:
            raise ValueError('Action outside fixed universe/calendar')
        payment = row.get('payable_date', row.get('pay_date'))
        records.append(dict(timestamp=opening[row['ex_date']], symbol=row['symbol'], split_ratio=1.,
                            cash_dividend=float(row['rate']),
                            payment_timestamp=pd.Timestamp(payment + ' 09:30',tz='America/New_York').tz_convert('UTC') if payment else pd.NaT,
                            source_id=row['id']))
    actions = pd.DataFrame(records).set_index('timestamp')
    if actions.source_id.duplicated().any():
        raise ValueError('Repeated action source identity')
    fresh = pd.read_parquet(refresh / 'dataset/actions.parquet')
    ids = set(actions.source_id)
    for timestamp, row in fresh.iterrows():
        if row.source_id in ids:
            old = actions.loc[actions.source_id == row.source_id].iloc[0]
            if old.name != timestamp or old.symbol != row.symbol or abs(old.cash_dividend-row.cash_dividend) > 1e-12:
                raise ValueError('Changed action requires review')
            continue
        actions = pd.concat([actions, pd.DataFrame([row], index=pd.DatetimeIndex([timestamp], name='timestamp'))])
        ids.add(row.source_id)
    actions = actions.sort_index(kind='stable')
    actions = provisional_actions(actions, provisional_qqq_correction)
    if provisional_qqq_correction:
        for symbol, frame in frames.items():
            frame['total_return_close'] = causal_total_return(frame, actions, symbol)
            frame['signal_scale'] = frame.close / frame.total_return_close
            for column in ('high', 'low', 'close'):
                frame['signal_' + column] = frame[column] / frame.signal_scale
    duplicate = actions.reset_index().duplicated(['timestamp','symbol'], keep=False)
    duplicate_records = actions.reset_index().loc[duplicate].to_dict('records')
    audit = dict(daily_sessions=len(dates), first=dates[0], last=dates[-1], symbols=list(SYMBOLS),
                 daily_calendar_complete=True, raw_execution_prices_verified=True,
                 action_rows=len(actions), missing_payment_dates=int(actions.payment_timestamp.isna().sum()),
                 ambiguous_same_symbol_ex_date=duplicate_records,
                 blockers=['QQQ 2022-09-19 has two source identities, equal distributions and different payment dates; unresolved duplicate versus separate distribution.',
                           '90 distributions in 2016-2019 have unknown payment dates; accrue receivables but never assume spendable cash.',
                           'Corporate-action publication times and historical bar arrival/revision provenance are unknown.',
                           'SPY 2026-03-20 adjusted-factor implied distribution differs from archived cash rate; adjustments are not independent confirmation.'],
                 historical_role='development_only_nonqualifying', qualification_allowed=False,
                 final_session_boundary='2026-09-23 is neither observed month-end nor week-end; no future calendar session is invented.',
                 source_hashes=source_hashes,
                 provisional_correction=dict(enabled=provisional_qqq_correction,
                    removed_source_id=QQQ_PROVISIONAL_REMOVAL if provisional_qqq_correction else None,
                    retained_source_id=QQQ_RETAINED,
                    evidence_url='https://www.invesco.com/us/financial-products/etfs/product-detail?audienceType=investors&productId=QQQ&ticker=QQQ',
                    evidence='Parent coordinator reports issuer-indexed row: ex 2022-09-19, record 2022-09-20, pay 2022-10-31, amount 0.51856. Direct open redirects, so full table was not retrieved. Provisional diagnostic correction only.',
                    total_return_method='forward raw-close plus corrected ex-date cash distributions; no inferred payment dates' if provisional_qqq_correction else 'provider adjustment=all archive',
                    timing='Accounting convention fixed before strategy outcomes'))
    output.mkdir(parents=True, exist_ok=False)
    (output / 'daily').mkdir()
    schedule.to_parquet(output / 'schedule.parquet', index=False)
    actions.to_parquet(output / 'actions.parquet')
    for symbol, frame in frames.items():
        frame.to_parquet(output / 'daily' / (symbol + '.parquet'))
    audit['files'] = {str(p.relative_to(output)):sha(p) for p in output.rglob('*.parquet')}
    (output / 'manifest.json').write_text(json.dumps(audit,indent=2,default=str)+'\n')
    return audit

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',default='artifacts/net_edge_20260923')
    parser.add_argument('--refresh',default='artifacts/ytd_execution_10000_20260923/inputs')
    parser.add_argument('--output',required=True)
    parser.add_argument('--provisional-qqq-correction',action='store_true')
    args=parser.parse_args()
    result=prepare(args.source,args.refresh,args.output,provisional_qqq_correction=args.provisional_qqq_correction)
    print(json.dumps({k:v for k,v in result.items() if k not in {'source_hashes','files'}},indent=2,default=str))
