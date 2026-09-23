"""Accelerated independent execution scenarios with shared causal Python signals.

A ctypes C++ ledger mirrors the existing ETF replay; parity is a required external
validation, not an assertion of independent market evidence. No broker or network.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import ctypes
import hashlib
import os
import platform
import subprocess
import tempfile
import threading
import numpy as np
import pandas as pd
from trader_engine.research.strategy_spec import StrategySpec,ETF_UNIVERSE
from trader_engine.research.etf_candidates import mean_reversion_session_decisions,momentum_decision

SYMBOLS=tuple(ETF_UNIVERSE)
DAILY_FIELDS=('equity','cash','cumulative_gross_pnl','cumulative_net_pnl','cumulative_fees',
              'cumulative_impact_cost','dividend_receivable','dividends_paid','trade_count',
              'open_positions','daily_halt','drawdown_halt')
SUMMARY_FIELDS=('final_equity','cash','gross_pnl','net_pnl','fees','modeled_impact',
                'dividend_receivable','dividends_paid','trade_count','entry_count',
                'open_positions','drawdown_halt','daily_halt_days','max_close_mark_drawdown',
                'data_complete','valuation_valid')
REASONS=('qualified','gap_stop','drawdown_halt','daily_loss_halt','session_flatten',
         'time_exit','five_session_exit','momentum_exit','stop','target')
_BUILD_LOCK=threading.Lock()


def _immutable(array,dtype):
    array=np.ascontiguousarray(array,dtype=dtype)
    return np.frombuffer(array.tobytes(),dtype=dtype).reshape(array.shape)


@dataclass(frozen=True)
class PreparedYTD:
    candidate_id: str
    schedule: pd.DataFrame
    timestamps: pd.DatetimeIndex
    sessions: tuple[str,...]
    day_closes: tuple[pd.Timestamp,...]
    epoch_minutes: np.ndarray
    day: np.ndarray
    offset: np.ndarray
    day_lengths: np.ndarray
    prices: np.ndarray
    mr: np.ndarray
    mom: np.ndarray
    action_ex: np.ndarray
    action_pay: np.ndarray
    action_symbol: np.ndarray
    action_values: np.ndarray
    daily_history_complete: bool


def _index(frame,label):
    if not isinstance(frame.index,pd.DatetimeIndex) or frame.index.tz is None or frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
        raise ValueError(label+' requires ordered unique timezone-aware index')


def prepare_ytd(frames,daily,schedule,actions,candidate_id,start='2026-01-01',end='2026-09-23'):
    """Prepare the complete supplied exchange grid without future coverage exclusions.

    Datetime bounds apply to New York session dates. Daily signal selection exactly
    follows the original engine: index calendar date < session, exact expected
    prior session match, otherwise no MOM signal and incomplete-history metadata.
    """
    spec=StrategySpec(candidate_id)
    if set(frames)!=set(SYMBOLS):raise ValueError('Complete five-symbol universe required')
    if spec.family=='MOM' and (daily is None or set(daily)!=set(SYMBOLS)):
        raise ValueError('MOM requires all five daily histories')
    schedule=schedule.copy()
    for col in ('market_open','market_close'):
        if col not in schedule:raise ValueError('Exchange schedule required')
        for value in schedule[col]:
            stamp=pd.Timestamp(value)
            if pd.isna(stamp) or stamp.tzinfo is None:raise ValueError('Schedule timestamps must be timezone-aware')
        schedule[col]=pd.to_datetime(schedule[col],utc=True)
    if schedule.market_open.duplicated().any() or not schedule.market_open.is_monotonic_increasing:
        raise ValueError('Unique chronological schedule required')
    dates=schedule.market_open.dt.tz_convert('America/New_York').dt.strftime('%Y-%m-%d')
    schedule=schedule.loc[(dates>=start)&(dates<=end)].copy()
    if schedule.empty:raise ValueError('No sessions in requested interval')
    if (schedule.market_close<=schedule.market_open).any():raise ValueError('Invalid session interval')
    if any(schedule.market_open.iloc[i]<=schedule.market_close.iloc[i-1] for i in range(1,len(schedule))):raise ValueError('Overlapping schedule')
    if any((t.value%60_000_000_000)!=0 for col in ('market_open','market_close') for t in schedule[col]):raise ValueError('Minute-aligned schedule required')
    grids=[pd.date_range(row.market_open,row.market_close-pd.Timedelta(minutes=1),freq='min') for row in schedule.itertuples()]
    times=grids[0].append(grids[1:]);D=len(grids);T=len(times)
    if T>np.iinfo(np.int32).max:raise ValueError('Timeline exceeds ABI')
    lengths=np.array([len(g) for g in grids],dtype=np.int32)
    days=np.repeat(np.arange(D,dtype=np.int32),lengths)
    offsets=np.concatenate([np.arange(n,dtype=np.int32) for n in lengths])
    prices=np.full((T,5,4),np.nan);mr=np.full((T,5,3),np.nan);mom=np.zeros((D,5,4))
    complete=True
    for k,symbol in enumerate(SYMBOLS):
        f=frames[symbol];_index(f,'Minute history')
        required=['open','high','low','close','volume']
        if not set(required)<=set(f):raise ValueError('Missing OHLCV')
        values=f[required].to_numpy(dtype=float)
        if not np.isfinite(values).all() or (values[:,:4]<=0).any() or (values[:,4]<0).any():raise ValueError('Invalid OHLCV')
        if ((f.low>f[['open','close']].min(axis=1))|(f.high<f[['open','close']].max(axis=1))).any():raise ValueError('Inconsistent OHLCV')
        aligned=f.reindex(times)
        prices[:,k,:]=aligned[['open','high','low','close']].to_numpy(dtype=float)
        base=0
        for d,(row,grid) in enumerate(zip(schedule.itertuples(),grids)):
            if spec.family=='MR':
                session=f.iloc[f.index.searchsorted(row.market_open):f.index.searchsorted(row.market_close)]
                decisions=mean_reversion_session_decisions(session,spec)
                locs=grid.get_indexer(session.index)
                for loc,decision in zip(locs,decisions.values()):
                    if loc>=0 and decision.eligible:
                        mr[base+loc,k]=[decision.score,decision.atr,decision.target if decision.target is not None else np.nan]
            base+=len(grid)
        if spec.family=='MOM':
            f=daily[symbol];_index(f,'Daily history')
            required={'high','low','close','total_return_close'}
            if not required<=set(f):raise ValueError('Daily total-return/ATR history missing')
            values=f[list(required)].to_numpy(dtype=float)
            if not np.isfinite(values).all() or (values<=0).any():raise ValueError('Invalid daily prices')
            if 'signal_scale' in f and (not np.isfinite(f.signal_scale).all() or (f.signal_scale<=0).any()):raise ValueError('Invalid daily scale')
            for d,(_,row) in enumerate(schedule.iterrows()):
                current=row.market_open.tz_convert('America/New_York').date()
                completed=f.loc[pd.to_datetime(f.index).date<current]
                previous=row.get('previous_session')
                if previous is None or pd.isna(previous):previous=schedule.market_open.iloc[d-1].tz_convert('America/New_York').date() if d else None
                if previous is None:complete=False
                if previous is not None and (completed.empty or pd.Timestamp(completed.index[-1]).date()!=pd.Timestamp(previous).date()):
                    complete=False;continue
                decision=momentum_decision(completed,spec)
                mom[d,k]=[float(decision.eligible),decision.score,decision.atr,float(decision.reason=='nonpositive_momentum')]
    ex=[];pay=[];symbols=[];vals=[]
    if actions is not None and not actions.empty:
        if not isinstance(actions.index,pd.DatetimeIndex) or actions.index.tz is None or not actions.index.is_monotonic_increasing:raise ValueError('Ordered timezone-aware corporate actions required')
        if not {'symbol','split_ratio','cash_dividend'}<=set(actions):raise ValueError('Corporate action fields missing')
        if actions.reset_index().duplicated([actions.index.name or 'index','symbol']).any():raise ValueError('Duplicate corporate action')
        for stamp,row in actions.iterrows():
            if pd.isna(stamp) or row.symbol not in SYMBOLS:raise ValueError('Invalid corporate action identity')
            ratio,div=float(row.split_ratio),float(row.cash_dividend)
            if not np.isfinite([ratio,div]).all() or ratio<=0 or div<0:raise ValueError('Invalid corporate action amounts')
            payment=row.get('payment_timestamp',pd.NaT)
            if pd.isna(payment):p=-1
            else:
                payment=pd.Timestamp(payment)
                if payment.tzinfo is None or payment<stamp:raise ValueError('Invalid dividend payment time')
                p=int(times.searchsorted(payment))
            ex.append(int(times.searchsorted(stamp)));pay.append(p);symbols.append(SYMBOLS.index(row.symbol));vals.append((ratio,div))
    return PreparedYTD(candidate_id,schedule.copy(deep=True),times,tuple(t[0].tz_convert('America/New_York').date().isoformat() for t in grids),
        tuple(schedule.market_close),_immutable(times.as_unit('ns').asi8//60_000_000_000,np.int64),_immutable(days,np.int32),
        _immutable(offsets,np.int32),_immutable(lengths,np.int32),_immutable(prices,np.float64),
        _immutable(mr,np.float64),_immutable(mom,np.float64),_immutable(ex,np.int32),_immutable(pay,np.int32),
        _immutable(symbols,np.int32),_immutable(np.array(vals).reshape(-1,2),np.float64),complete)


def compile_kernel(cache_path=None):
    """Content-addressed local C++17 build; no fast-math or FP contraction."""
    source=Path(__file__).resolve().parents[1]/'backtest'/'etf_sensitivity_core.cpp'
    flags=['-std=c++17','-O3','-ffp-contract=off','-shared','-fPIC']
    digest=hashlib.sha256(source.read_bytes()+repr((flags,platform.platform(),platform.machine())).encode()).hexdigest()
    root=Path(cache_path) if cache_path else Path(tempfile.gettempdir())/'trader2-etf-kernel'
    root.mkdir(parents=True,exist_ok=True)
    target=root/(digest+('.dylib' if platform.system()=='Darwin' else '.so'))
    with _BUILD_LOCK:
        if not target.exists():
            temp=root/(digest+f'.{os.getpid()}.tmp')
            try:
                subprocess.run(['clang++',*flags,str(source),'-o',str(temp)],check=True,capture_output=True,text=True)
                os.replace(temp,target)
            finally:
                if temp.exists():temp.unlink()
    return target


def _load_library(library=None,cache_path=None):
    lib=library if isinstance(library,ctypes.CDLL) else ctypes.CDLL(str(library or compile_kernel(cache_path)))
    dp=ctypes.POINTER(ctypes.c_double);ip=ctypes.POINTER(ctypes.c_int);lp=ctypes.POINTER(ctypes.c_longlong)
    lib.etf_scenario.argtypes=[ctypes.c_int]*6+[ctypes.c_double]*2+[dp,lp,ip,ip,ip,dp,dp,dp,ip,ip,ip,dp,dp,dp,dp,ctypes.c_int]
    lib.etf_scenario.restype=ctypes.c_int
    return lib


@dataclass(frozen=True)
class ScenarioResult:
    metrics: dict
    daily: pd.DataFrame
    events: pd.DataFrame


def compiled_library(cache_dir=None):
    return _load_library(cache_path=cache_dir)


def run_scenario(prepared,spec,delay,library=None,cache_path=None):
    """One independent $100k full-universe ledger; ctypes releases GIL in kernel.

    `library` accepts a compiled path or loaded CDLL. Each call owns all output
    buffers. Unsupported costs/overhead or candidate changes are rejected.
    """
    if not isinstance(prepared,PreparedYTD) or spec.candidate_id!=prepared.candidate_id:raise ValueError('Prepared strategy mismatch')
    if tuple(spec.universe)!=SYMBOLS:raise ValueError('Complete frozen universe required')
    if spec.daily_overhead not in (None,0):raise ValueError('Nonzero overhead unsupported in sensitivity kernel')
    if isinstance(delay,bool) or not isinstance(delay,int) or not 0<=delay<=60:raise ValueError('Invalid execution delay')
    lib=_load_library(library,cache_path);D=len(prepared.sessions);T=len(prepared.timestamps);A=len(prepared.action_ex)
    limits=np.array([spec.max_trade_risk,spec.max_portfolio_risk,spec.max_gross,spec.max_name,
        spec.max_equity_cluster,spec.max_overnight,spec.daily_loss_halt,spec.drawdown_halt],dtype=np.float64)
    out=np.zeros((D,12));summary=np.zeros(16);capacity=10*D+10;events=np.zeros((capacity,10))
    ptr=lambda x:x.ctypes.data_as(ctypes.POINTER(ctypes.c_double))
    iptr=lambda x:x.ctypes.data_as(ctypes.POINTER(ctypes.c_int))
    count=lib.etf_scenario(T,D,A,0 if spec.family=='MR' else 1,spec.horizon,delay,spec.one_way_impact,spec.commission_rate,
        ptr(limits),prepared.epoch_minutes.ctypes.data_as(ctypes.POINTER(ctypes.c_longlong)),
        iptr(prepared.day),iptr(prepared.offset),iptr(prepared.day_lengths),ptr(prepared.prices),ptr(prepared.mr),ptr(prepared.mom),
        iptr(prepared.action_ex),iptr(prepared.action_pay),iptr(prepared.action_symbol),ptr(prepared.action_values),ptr(out),ptr(summary),ptr(events),capacity)
    if count<0:raise RuntimeError(f'ETF kernel rejected scenario or event capacity: {count}')
    if count>capacity or not np.isfinite(out).all() or not np.isfinite(summary).all():raise RuntimeError('Invalid kernel result')
    metrics=dict(zip(SUMMARY_FIELDS,summary.tolist()))
    for name in ['trade_count','entry_count','open_positions','daily_halt_days']:metrics[name]=int(metrics[name])
    for name in ['drawdown_halt','data_complete','valuation_valid']:metrics[name]=bool(metrics[name])
    metrics['data_complete']=metrics['data_complete'] and prepared.daily_history_complete
    metrics.update(spec_hash=spec.spec_hash,execution_delay_minutes=delay,sessions=D,
        overhead_known=spec.daily_overhead is not None,business_pnl=None if spec.daily_overhead is None else metrics['net_pnl'],
        overhead=0.,mode='historical_execution_sensitivity',broker_orders_submitted=0)
    table=pd.DataFrame(out,columns=DAILY_FIELDS);table.insert(0,'session',prepared.sessions);table['timestamp']=prepared.day_closes
    for cumulative,name in [('cumulative_gross_pnl','gross_pnl'),('cumulative_net_pnl','net_pnl')]:table[name]=table[cumulative].diff().fillna(table[cumulative])
    records=pd.DataFrame(events[:count],columns=['time_index','symbol_index','side_code','reason_code','quantity','raw_price','price','fee','episode_net','episode_gross'])
    records['timestamp']=[prepared.timestamps[int(i)] for i in records.time_index]
    records['symbol']=[SYMBOLS[int(i)] for i in records.symbol_index]
    records['action']=['entry' if i==1 else 'exit' for i in records.side_code]
    records['reason']=[REASONS[int(i)] for i in records.reason_code]
    return ScenarioResult(metrics,table,records)
