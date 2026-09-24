"""Continuous Plus data intake and explicit decision diagnostics; never sends orders."""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import shutil
import signal
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from market_scanner_service import atomic, credentials
from trader_engine.data.plus_rest import PlusRESTClient, assess_option_candidates
from trader_engine.execution.journal import ExecutionLock
from trader_engine.research.plus_decisions import select_universe, evaluate, merge_stream_quotes


def read(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def order_contract_sample(groups, quotes):
    """Balanced collection sample, prioritizing near-money contracts; not a signal."""
    prices = {}
    for row in quotes.get('records', []):
        try:
            mid = (float(row['bp'])+float(row['ap']))/2
            if math.isfinite(mid) and mid > 0:
                prices[row['symbol']] = mid
        except (KeyError, ValueError, TypeError):
            pass
    buckets = []
    for underlying, rows in groups:
        price = prices.get(underlying)
        def distance(row):
            try:
                strike = float(row['strike_price'])
                return abs(strike/price-1) if price and math.isfinite(strike) and strike > 0 else float('inf')
            except (KeyError, ValueError, TypeError):
                return float('inf')
        buckets.append(sorted(rows, key=lambda r: (distance(r), str(r.get('expiration_date', '')), r.get('symbol',''))))
    result = []
    for index in range(max((len(b) for b in buckets), default=0)):
        for bucket in buckets:
            if index < len(bucket):
                result.append(bucket[index])
    return result


class ResearchService:
    def __init__(self, client, directory, artifacts):
        self.client, self.directory, self.artifacts = client, Path(directory), Path(artifacts)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.options_at = 0.

    def cycle(self):
        now = datetime.now(timezone.utc)
        if shutil.disk_usage(self.directory).free < 1024**3:
            raise RuntimeError('disk_headroom_below_1GiB')
        status = dict(checked_at=now.isoformat(), status='evaluating', mode='research_only',
                      broker_execution_enabled=False, errors=[])
        atomic(self.directory/'status.json', status)
        universe = select_universe(read(self.artifacts/'continuous_market_scan/latest.json'), now)
        # End is the current minute boundary: the forming bar never enters a signal.
        end = now.replace(second=0, microsecond=0)
        bars = self.client.bars(universe, (end-timedelta(minutes=30)).isoformat(),
                                end.isoformat(), timeframe='1Min', adjustment='raw')
        quotes = self.client.latest_quotes(universe)
        decision_time = datetime.now(timezone.utc)
        quotes = merge_stream_quotes(quotes, read(self.artifacts/'plus_stream/latest.json'), decision_time)
        decisions = evaluate(universe, bars, quotes, decision_time)
        atomic(self.directory/'decisions.json', dict(checked_at=decision_time.isoformat(),
               mode='research_only', records=decisions, universe=universe,
               historical_provenance=bars.get('provenance'), quote_provenance=quotes.get('provenance')))
        atomic(self.directory/'inputs.json', dict(bars=bars, quotes=quotes))
        for result in (bars, quotes):
            status['errors'].extend(result.get('errors', []))
        # OPRA refresh has its own cadence; contracts are evidence, never order targets.
        if time.monotonic()-self.options_at >= 300:
            self.options_at = time.monotonic()
            option_rows, sources, contract_groups = [], [], []
            start, end_date = now.date().isoformat(), (now+timedelta(days=45)).date().isoformat()
            for underlying in ('SPY', 'QQQ', 'IWM'):
                status.update(checked_at=datetime.now(timezone.utc).isoformat(), stage='options_'+underlying)
                atomic(self.directory/'status.json', status)
                chain = self.client.option_chain(underlying, expiration_start=start, expiration_end=end_date)
                meta = self.client.option_contracts(underlying, expiration_start=start, expiration_end=end_date)
                assessment = assess_option_candidates(chain, datetime.now(timezone.utc), contracts=meta)
                rows = assessment['records']
                option_rows.extend(rows)
                contract_groups.append((underlying, meta.get('records', [])))
                sources.append(dict(underlying=underlying, chain=chain.get('provenance'),
                                    complete=chain.get('complete'), errors=chain.get('errors', []),
                                    assessed_count=assessment['assessed_count'], input_count=assessment['input_count'],
                                    assessment_truncated=assessment['truncated']))
                status['errors'].extend(chain.get('errors', []))
                status['errors'].extend(meta.get('errors', []))
            atomic(self.directory/'options.json', dict(checked_at=datetime.now(timezone.utc).isoformat(),
                   feed='opra', records=option_rows, contracts=order_contract_sample(contract_groups, quotes), sources=sources,
                   stream_selection='balanced_underlyings_near_money_collection_sample_not_signal',
                   broker_execution_enabled=False))
        status.update(checked_at=datetime.now(timezone.utc).isoformat(),
                      status='degraded' if status['errors'] else 'running',
                      stage='cycle_complete', cycle_seconds=(datetime.now(timezone.utc)-now).total_seconds(), pause_after_cycle_seconds=60,
                      universe=universe, evaluated=len(decisions),
                      signals=sum(row['signal'] is not None for row in decisions),
                      execution_block='No approved equity/options execution rule; crypto has its separate owner')
        atomic(self.directory/'status.json', status)
        return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=ROOT/'artifacts/plus_research')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--artifacts', type=Path, default=ROOT/'artifacts')
    args = parser.parse_args()
    key, secret = credentials()
    tag = hashlib.sha256(key.encode()).hexdigest()[:24]
    stop = [False]
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.__setitem__(0, True))
    service = ResearchService(PlusRESTClient(key, secret, max_pages=10), args.directory, args.artifacts)
    lock = Path(tempfile.gettempdir())/('trader-engine-plus-research-'+tag+'.lock')
    with ExecutionLock(lock):
        while not stop[0] and not (args.directory/'SHUTDOWN').exists():
            try:
                service.cycle()
            except Exception as exc:
                atomic(args.directory/'status.json', dict(checked_at=datetime.now(timezone.utc).isoformat(),
                       status='error', mode='research_only', error=type(exc).__name__, broker_execution_enabled=False))
            if args.once:
                return
            deadline = time.monotonic()+60
            while time.monotonic() < deadline and not stop[0] and not (args.directory/'SHUTDOWN').exists():
                time.sleep(1)
        atomic(args.directory/'status.json', dict(checked_at=datetime.now(timezone.utc).isoformat(), status='shutdown',mode='research_only'))


if __name__ == '__main__':
    main()
