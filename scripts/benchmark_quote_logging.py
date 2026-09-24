"""Bounded offline quote evidence timing; never opens a broker or runs a runner.

Temporary evidence is created on the project volume and removed afterward.
Run with .venv/bin/python scripts/benchmark_quote_logging.py --output DIRECTORY.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import random
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from trader_engine.execution.journal import ReservedJournal


def summary(values):
    ordered = sorted(values)
    return {**{f'p{p}_ms': ordered[math.ceil(len(ordered) * p / 100) - 1]
               for p in (50, 95, 99)}, 'max_ms': ordered[-1]}


def benchmark(samples=300, warmup=20):
    if not 20 <= samples <= 500 or not 1 <= warmup <= 50:
        raise ValueError('Use 20..500 samples and 1..50 warmup rounds')
    runner_path = ROOT / 'scripts/paper_breakout_until_close.py'
    spec = importlib.util.spec_from_file_location('benchmark_quote_helper', runner_path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)  # Import helpers only; never call run()/main().
    now = datetime(2026, 9, 23, 19, 15, tzinfo=timezone.utc)
    quote = {'t': now.isoformat(), 'bp': 100, 'ap': 100.01, 'bs': 10, 'as': 10}
    source_paths = [runner_path, ROOT / 'src/trader_engine/data/quote_validation.py',
                    ROOT / 'src/trader_engine/execution/journal.py', Path(__file__)]
    source_hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in source_paths}
    seed = 20260923
    rng = random.Random(seed)
    rows = []
    # os.fsync is still the real system call; timing adds two clock reads per call.
    original_fsync = os.fsync
    sync_times = []

    def timed_fsync(fd):
        started = time.perf_counter_ns()
        try:
            return original_fsync(fd)
        finally:
            sync_times.append((time.perf_counter_ns() - started) / 1e6)

    with tempfile.TemporaryDirectory(prefix='.quote-benchmark-', dir=ROOT) as temporary:
        directory = Path(temporary)
        with ReservedJournal(directory / 'quotes.journal', capacity=2048) as journal:
            jsonl = directory / 'quotes.jsonl'
            cases = {
                'validation_only': None,
                'validation_jsonl_fsync': lambda row: runner.record_quote_decision(jsonl, row),
                'validation_reserved_2048_fsync': journal.append,
            }
            for index in range(warmup + samples):
                names = list(cases)
                rng.shuffle(names)
                for name in names:
                    sync_times.clear()
                    with patch('os.fsync', timed_fsync):
                        started = time.perf_counter_ns()
                        prices = runner.qualified_quote(quote, now, symbol='XOM',
                            received_at=now.isoformat(), phase='entry_submission',
                            evidence=cases[name])
                        elapsed = (time.perf_counter_ns() - started) / 1e6
                    if prices is None:
                        raise AssertionError('Synthetic quote unexpectedly rejected')
                    expected_syncs = 0 if name == 'validation_only' else 1
                    if len(sync_times) != expected_syncs:
                        raise AssertionError('Measured path changed its fsync count')
                    if index >= warmup:
                        rows.append({'sample': index - warmup, 'case': name,
                                     'elapsed_ms': elapsed,
                                     'fsync_ms': sum(sync_times),
                                     'fsync_calls': len(sync_times),
                                     'journal_records_before': index if 'reserved' in name else None})
            stat = os.stat(directory)
            volume = os.statvfs(directory)
            record = journal.records()[0]
    for path in source_paths:
        if source_hashes[str(path.relative_to(ROOT))] != hashlib.sha256(path.read_bytes()).hexdigest():
            raise RuntimeError('Measured source changed during benchmark; rerun when stable')
    summaries = {}
    for name in cases:
        selected = [r for r in rows if r['case'] == name]
        summaries[name] = {'n': len(selected), **summary([r['elapsed_ms'] for r in selected]),
                           'fsync': summary([r['fsync_ms'] for r in selected])}
    baseline = summaries['validation_only']
    for name in cases:
        summaries[name]['percentile_difference_from_baseline_ms'] = {
            key: summaries[name][key] - baseline[key]
            for key in ('p50_ms', 'p95_ms', 'p99_ms', 'max_ms')}
    return {
        'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'provenance': 'offline_synthetic_quote_local_filesystem_only',
        'environment': {'python': sys.version, 'platform': platform.platform(),
                        'machine': platform.machine(), 'cpu_count': os.cpu_count(),
                        'project_path': str(ROOT), 'filesystem_device': stat.st_dev,
                        'filesystem_block_size': volume.f_bsize,
                        'filesystem_free_bytes': volume.f_bavail * volume.f_frsize,
                        'clock': vars(time.get_clock_info('perf_counter'))},
        'method': {'samples_per_case': samples, 'warmup_per_case': warmup,
                   'case_order': 'seeded shuffle each round', 'random_seed': seed,
                   'percentiles': 'nearest rank', 'journal_capacity': 2048,
                   'journal_slot_bytes': 4096,
                   'journal_records_before_measured_range': [warmup, warmup + samples - 1],
                   'record_json_bytes': len(json.dumps(record, sort_keys=True, allow_nan=False).encode()),
                   'initialization_included': False,
                   'fsync_instrumentation': 'real os.fsync timed inside total; two clock reads per call',
                   'limits': ['No broker, network, credentials, order submission, or runner execution.',
                              'Single process; excludes live contention, cold cache and full order lifecycle.',
                              'Journal append reloads every reserved slot before writing.',
                              'Percentile differences are descriptive, not paired latency distributions.',
                              'Local measurements do not bound future latency or establish hardware power-loss durability.']},
        'source_sha256': source_hashes, 'synthetic_quote': quote,
        'summary': summaries, 'samples': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples', type=int, default=300)
    parser.add_argument('--warmup', type=int, default=20)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Choose a new output directory to preserve earlier evidence')
    result = benchmark(args.samples, args.warmup)
    args.output.mkdir(parents=True)
    (args.output / 'results.json').write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
    print(json.dumps(result['summary'], indent=2))


if __name__ == '__main__':
    main()
