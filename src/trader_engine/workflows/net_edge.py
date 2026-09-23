"""Offline orchestration for the shared ETF engine. No provider or broker clients."""
from __future__ import annotations
from calendar import monthrange
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from pathlib import Path
import json
import math
import numpy as np
import pandas as pd
from trader_engine.research.strategy_spec import StrategySpec, CANDIDATE_IDS, ETF_UNIVERSE
from trader_engine.research.net_edge import (canonical_hash,file_hash,EvidenceManifest,
    load_confirmation_inputs,assess_net_edge_confirmation,stationary_bootstrap_bounds,select_champion)
from trader_engine.research.walk_forward import build_session_folds,run_etf_continuous
from trader_engine.backtest.engine import BacktestEngine


def write_json(path,value):
    Path(path).write_text(json.dumps(value,indent=2,allow_nan=False,default=lambda v:v.isoformat() if hasattr(v,'isoformat') else str(v)))


def artifact_index(root):
    root=Path(root)
    files={str(p.relative_to(root)):file_hash(p) for p in sorted(root.rglob("*")) if p.is_file() and p.name!="artifact_index.json"}
    write_json(root/"artifact_index.json",dict(files=files,broker_orders_submitted=0,approved_for_trading=False))


def source_hash():
    root=Path(__file__).resolve().parents[1]
    return canonical_hash({str(p.relative_to(root)):file_hash(p) for p in sorted(root.rglob('*.py'))})


def freeze_registry(output, *, monthly_overhead=None, frozen_at=None):
    if monthly_overhead is not None and (not math.isfinite(monthly_overhead) or monthly_overhead<0):raise ValueError('Nonnegative known overhead or explicit unknown required')
    stamp=datetime.now(timezone.utc) if frozen_at is None else datetime.fromisoformat(frozen_at.replace('Z','+00:00'))
    if stamp.tzinfo is None:raise ValueError('Freeze timestamp must include timezone')
    if stamp>datetime.now(timezone.utc):raise ValueError('Freeze cannot be future-dated')
    payload=dict(schema_version=1,mode='historical_development',frozen_at=stamp.isoformat(),
        code_hash=source_hash(),operating_cost_monthly=monthly_overhead,
        overhead_status='unknown' if monthly_overhead is None else 'known',
        specs=[dict(spec=asdict(StrategySpec(k)),spec_hash=StrategySpec(k).spec_hash) for k in CANDIDATE_IDS],
        costs_bps=[1,3,7,14,28],adverse_delay_minutes=1,minimum_oos_sessions=126,
        approved_for_trading=False,broker_orders_submitted=0)
    payload['registry_hash']=canonical_hash(payload)
    path=Path(output)
    if path.exists():raise FileExistsError('Registry is immutable; choose a new path')
    path.parent.mkdir(parents=True,exist_ok=True);write_json(path,payload);return payload


def load_registry(path):
    data=json.loads(Path(path).read_text());stored=data.pop('registry_hash')
    if canonical_hash(data)!=stored:raise ValueError('Registry hash mismatch')
    data['registry_hash']=stored
    if data['code_hash']!=source_hash():raise ValueError('Code changed since freeze; freeze a new development registry')
    specs=[]
    for item in data['specs']:
        values=item['spec']|{'universe':tuple(item['spec']['universe'])};spec=StrategySpec(**values)
        if spec.spec_hash!=item['spec_hash']:raise ValueError('Strategy hash mismatch')
        specs.append(spec)
    if tuple(s.candidate_id for s in specs)!=CANDIDATE_IDS:raise ValueError('Frozen four-candidate registry required')
    return data,specs


def load_dataset(path):
    root=Path(path).resolve();manifest=json.loads((root/'manifest.json').read_text())
    hashes=manifest.get('hashes',{})
    required={'schedule.parquet'}|{f'minutes/{s}.parquet' for s in ETF_UNIVERSE}|{f'daily/{s}.parquet' for s in ETF_UNIVERSE}
    if not required<=set(hashes):raise ValueError('Missing hashed dataset files: '+str(sorted(required-set(hashes))))
    for name,digest in hashes.items():
        target=root/name
        if Path(name).is_absolute() or '..' in Path(name).parts or not target.resolve().is_relative_to(root):raise ValueError('Unsafe manifest path')
        if not target.is_file() or file_hash(target)!=digest:raise ValueError('Dataset integrity failure: '+name)
    frames={s:pd.read_parquet(root/f'minutes/{s}.parquet') for s in ETF_UNIVERSE}
    daily={s:pd.read_parquet(root/f'daily/{s}.parquet') for s in ETF_UNIVERSE}
    for s,f in daily.items():
        if not {'high','low','close','total_return_close','signal_scale'}<=set(f):raise ValueError('Daily price contract missing: '+s)
        if not isinstance(f.index,pd.DatetimeIndex) or f.index.tz is None or not f.index.is_monotonic_increasing or f.index.has_duplicates:raise ValueError('Daily index must be unique ordered timezone-aware completed timestamps')
    schedule=pd.read_parquet(root/'schedule.parquet')
    if not {'market_open','market_close'}<=set(schedule):raise ValueError('Schedule missing exchange open/close')
    schedule=schedule.copy()
    for k in ['market_open','market_close']:schedule[k]=pd.to_datetime(schedule[k],utc=True)
    if schedule.empty or not schedule.market_open.is_monotonic_increasing or schedule.market_open.duplicated().any():raise ValueError('Invalid chronological schedule')
    actions=pd.read_parquet(root/'actions.parquet') if 'actions.parquet' in hashes else None
    return manifest,frames,daily,schedule,actions


def calendar_costs(schedule,monthly):
    if monthly is None:return None
    first=schedule.market_open.iloc[0].tz_convert('America/New_York').date()
    last=schedule.market_open.iloc[-1].tz_convert('America/New_York').date()
    return {t.date():monthly/monthrange(t.year,t.month)[1] for t in pd.date_range(first,last)}


def daily_ledger(result):
    f=result.equity_curve.copy()
    if f.empty:return pd.DataFrame()
    f['session']=pd.to_datetime(f.timestamp,utc=True).dt.tz_convert('America/New_York').dt.strftime('%Y-%m-%d')
    out=f.groupby('session',sort=True).tail(1).set_index('session')
    for cumulative,label in [('cumulative_gross_pnl','gross_pnl'),('cumulative_net_pnl','net_pnl'),('overhead','operating_cost')]:
        out[label]=out[cumulative].diff().fillna(out[cumulative])
    out['business_pnl']=out.net_pnl-out.operating_cost
    out['execution_cost']=out.gross_pnl-out.net_pnl
    out['business_equity']=out.equity
    out['equity']=out.broker_equity
    counts={}
    if not result.trades.empty:
        col='exit_time' if 'exit_time' in result.trades else 'timestamp'
        if col in result.trades:
            counts=pd.to_datetime(result.trades[col],utc=True).dt.tz_convert('America/New_York').dt.strftime('%Y-%m-%d').value_counts().to_dict()
    out['closed_episodes']=[counts.get(s,0) for s in out.index]
    return out.reset_index()


def save_replay(root,result):
    root.mkdir(parents=True)
    result.equity_curve.to_parquet(root/'ledger.parquet',index=False)
    result.decisions.to_parquet(root/'decisions.parquet',index=False)
    result.trades.to_parquet(root/'trades.parquet',index=False)
    result.symbol_contributions.to_parquet(root/'symbol_contributions.parquet',index=False)
    ledger=daily_ledger(result);ledger.to_parquet(root/'daily.parquet',index=False)
    write_json(root/'metrics.json',result.metrics)
    return ledger


def benchmarks(frames,schedule,actions,monthly,cost_bps=7):
    """50% initial equal-weight buy/hold, terminal hypothetical liquidation costs.

    Raw-price split/dividend accounting; idle cash and basket both accrue overhead.
    No forced daily rebalance. Missing marks fail rather than inventing prices.
    """
    cash=100000.;qty={};cost=cost_bps/10000;overhead=0.;charged=set();rows=[]
    costs=calendar_costs(schedule,monthly);action_cursor=0
    events=[] if actions is None or actions.empty else list(actions.sort_index().iterrows())
    for i,row in enumerate(schedule.itertuples()):
        op,cl=row.market_open,row.market_close
        if costs is not None:
            for d,value in costs.items():
                if d<=op.tz_convert('America/New_York').date() and d not in charged:overhead+=value;charged.add(d)
        marks={}
        for symbol,frame in frames.items():
            bars=frame.loc[(frame.index>=op)&(frame.index<cl)]
            if bars.empty:raise ValueError('Benchmark missing symbol-session '+symbol)
            if i==0:
                px=float(bars.iloc[0].open);qty[symbol]=10000/(px*(1+cost));cash-=10000
            marks[symbol]=float(bars.iloc[-1].close)
        while action_cursor<len(events) and events[action_cursor][0]<cl:
            stamp,act=events[action_cursor];action_cursor+=1
            # First-session pre-open actions predate initial purchase.
            if stamp<=schedule.market_open.iloc[0]:continue
            symbol=act.symbol;qty[symbol]*=float(act.split_ratio);cash+=qty[symbol]*float(act.cash_dividend)
        gross=sum(qty[s]*marks[s] for s in qty)
        equity=cash+gross-(gross*cost if i==len(schedule)-1 else 0)-overhead
        rows.append(dict(session=op.tz_convert('America/New_York').date().isoformat(),cash_equity=100000-overhead,basket_equity=equity,overhead_known=monthly is not None))
    return pd.DataFrame(rows)


def run_research(data_dir,registry_path,output,*,diagnostic_last=None):
    registry,specs=load_registry(registry_path)
    manifest,frames,daily,schedule,actions=load_dataset(data_dir)
    if diagnostic_last is not None and diagnostic_last<1:raise ValueError('Diagnostic lastN must be positive')
    folds=build_session_folds(pd.to_datetime(schedule.market_open,utc=True).dt.tz_convert('America/New_York').dt.date)
    diagnostic=diagnostic_last is not None or not folds
    if diagnostic_last is not None:schedule=schedule.tail(diagnostic_last).copy()
    if not diagnostic:
        first=min(d for f in folds for d in f.test_sessions);last=max(d for f in folds for d in f.test_sessions)
        dates=schedule.market_open.dt.tz_convert('America/New_York').dt.date
        active=schedule.loc[(dates>=first)&(dates<=last)]
    else:active=schedule
    monthly=registry['operating_cost_monthly'];costs=calendar_costs(active,monthly)
    output=Path(output)
    if output.exists():raise FileExistsError('Choose new research output directory')
    output.mkdir(parents=True)
    write_json(output/'registry.json',registry);write_json(output/'data_manifest.json',manifest)
    common=dict(daily_signal_frames=daily,corporate_actions=actions,calendar_overhead=costs)
    def replay(spec,**extra):
        if diagnostic:return BacktestEngine.run_etf_replay(frames,spec,schedule=active,**common,**extra)
        return run_etf_continuous(frames,spec,schedule=schedule,folds=folds,**common,**extra)
    summary=[]
    for base in specs:
        runs={};ledgers={};candidate=output/base.candidate_id
        for bps,delay in [(1,0),(3,0),(7,0),(14,0),(28,0),(14,1)]:
            label=f'cost_{bps}bps_delay_{delay}m';spec=replace(base,slippage_bps=float(bps))
            result=replay(spec,execution_delay_minutes=delay);runs[(bps,delay)]=result
            ledgers[(bps,delay)]=save_replay(candidate/label,result)
        primary=runs[(7,0)];ledger=ledgers[(7,0)]
        why=[];positive=all(r.metrics.get('business_pnl') is not None and r.metrics['business_pnl']>0 for k,r in runs.items() if k in [(7,0),(14,0),(14,1)])
        if diagnostic:why.append('diagnostic_not_qualification')
        if manifest.get('coverage_complete') is not True:why.append('dataset_coverage_unverified')
        if any(r.metrics.get('valuation_valid') is not True for r in runs.values()):why.append('valuation_invalid_or_unverified')
        if any(r.metrics.get('data_complete') is not True for r in runs.values()):why.append('incomplete_or_unverified_replay_data')
        if len(ledger)<126:why.append('insufficient_oos_sessions')
        if monthly is None:why.append('unknown_operating_overhead')
        if actions is None or manifest.get('corporate_actions_verified') is not True:why.append('corporate_actions_unverified')
        if manifest.get('cost_calibration_verified') is not True:why.append('execution_cost_calibration_unverified')
        if not positive:why.append('nonpositive_or_unknown_net_economics')
        if primary.metrics['trade_count']<30:why.append('insufficient_closed_episodes')
        if not ledger.empty and ledger.business_pnl.sum()-ledger.business_pnl.nlargest(5).sum()<=0:why.append('best_five_days_concentration')
        path=np.r_[100000,primary.equity_curve.equity.to_numpy()]
        dd=float(np.max(1-path/np.maximum.accumulate(path)))
        if dd>.03+1e-12:why.append('drawdown_exceeded')
        worst=None
        if not why:
            loo={}
            for symbol in ETF_UNIVERSE:
                r=replay(replace(base,slippage_bps=14.),excluded_symbols=(symbol,))
                save_replay(candidate/('exclude_'+symbol),r);loo[symbol]=r.metrics['business_pnl']
            if any(v is None or v<=0 for v in loo.values()):why.append('leave_one_etf_out_failed')
            if not why:
                vectors=np.column_stack([ledgers[k].business_pnl.to_numpy() for k in [(7,0),(14,0),(14,1)]])
                bounds=stationary_bootstrap_bounds(vectors);worst=min(min(v) for v in bounds.values())
                write_json(candidate/'bootstrap.json',dict(lower_bounds=bounds,resamples=100000,alpha=.05/6,blocks=[5,10],seed=20260923))
                if worst<=0:why.append('uncertainty_not_resolved')
        executions=primary.decisions.loc[primary.decisions.action.isin(['entry','exit'])] if not primary.decisions.empty else pd.DataFrame()
        turnover=float((executions.price*executions.quantity).sum()/100000) if not executions.empty else 0.
        summary.append(dict(candidate_id=base.candidate_id,development_passed=not why,reasons=why,
            worst_lower_bound=worst,max_drawdown=dd,turnover=turnover,metrics=primary.metrics))
    benchmark=benchmarks(frames,active,actions,monthly);benchmark.to_parquet(output/'benchmarks_7bps.parquet',index=False)
    champion=select_champion(summary)
    report=dict(mode='diagnostic' if diagnostic else 'historical_development',registry_hash=registry['registry_hash'],
        diagnostic_reason='requested_lastN' if diagnostic_last else 'insufficient_complete_folds' if diagnostic else None,
        candidates=summary,champion=champion,approved_for_trading=False,broker_orders_submitted=0,
        note='Historical selection is development only. Unknown overhead/calibration/actions prevent qualification. No confirmation or orders.')
    write_json(output/'summary.json',report);artifact_index(output);return report


def review_bundle(bundle,*,as_of):
    root=Path(bundle);evidence=EvidenceManifest.from_dict(json.loads((root/'evidence_manifest.json').read_text()))
    # Check before any artifact parsing.
    failures=evidence.verify(root)
    if failures:raise ValueError('Evidence integrity failure: '+','.join(failures))
    return assess_net_edge_confirmation(**load_confirmation_inputs(root),evidence=evidence,evidence_root=root,as_of=as_of).to_dict()


def analyze_snapshot(path):
    from trader_engine.agents import Evidence,SharedContext,run_team
    raw=json.loads(Path(path).read_text())
    dt=lambda value:datetime.fromisoformat(value.replace('Z','+00:00'))
    evidence=[]
    for e in raw.get('evidence',[]):
        converted=e|{k:dt(e[k]) for k in ['observed_at','published_at','available_at']}
        if e.get('vintage_at') is not None:converted['vintage_at']=dt(e['vintage_at'])
        evidence.append(Evidence(**converted))
    context=SharedContext(as_of=dt(raw['as_of']),strategy_hash=raw['strategy_hash'],symbols=tuple(raw['symbols']),
        evidence=tuple(evidence),max_age_seconds=raw.get('max_age_seconds',86400),max_age_by_kind=raw.get('max_age_by_kind',{}))
    report=asdict(run_team(context))
    report['method']='deterministic_evidence_analysis'
    report['provider_configured']=False
    report['evidence_sources']={e.source_id:dict(source_url=e.source_url,kind=e.kind,observed_at=e.observed_at.isoformat(),published_at=e.published_at.isoformat(),available_at=e.available_at.isoformat(),vintage_at=e.vintage_at.isoformat() if e.vintage_at else None) for e in evidence}
    return report


def run_shadow_snapshot(snapshot,registry_path,candidate_id,output):
    """Read a local JSON snapshot and produce diagnostic decisions, never orders.

    Referenced parquet/risk files are relative to the snapshot directory and may
    not escape it. Risk observations mutate a temporary copy, never source state.
    """
    from tempfile import TemporaryDirectory
    from trader_engine.agents import Evidence,SharedContext
    from trader_engine.execution.account_risk import AccountRisk
    from trader_engine.execution.shadow import run_shadow
    from trader_engine.data.quote_validation import QuoteEnvelope,validate_quote
    registry,specs=load_registry(registry_path)
    spec=next((s for s in specs if s.candidate_id==candidate_id),None)
    if spec is None:raise ValueError('Unknown frozen candidate')
    snapshot=Path(snapshot).resolve();base=snapshot.parent;raw=json.loads(snapshot.read_text())
    hashes={snapshot.name:file_hash(snapshot)}
    def local(name):
        path=base/name
        if Path(name).is_absolute() or '..' in Path(name).parts or not path.resolve().is_relative_to(base):raise ValueError('Snapshot references must stay inside snapshot directory')
        if not path.is_file():raise ValueError('Missing snapshot input '+name)
        hashes[name]=file_hash(path);return path
    dt=lambda value:datetime.fromisoformat(value.replace('Z','+00:00'))
    c=raw['context'];evidence=[]
    if c['strategy_hash']!=spec.spec_hash:raise ValueError('Snapshot does not match frozen candidate')
    for e in c.get('evidence',[]):
        converted=e|{k:dt(e[k]) for k in ['observed_at','published_at','available_at']}
        if e.get('vintage_at') is not None:converted['vintage_at']=dt(e['vintage_at'])
        evidence.append(Evidence(**converted))
    context=SharedContext(as_of=dt(c['as_of']),strategy_hash=c['strategy_hash'],symbols=tuple(c['symbols']),
        evidence=tuple(evidence),max_age_seconds=c.get('max_age_seconds',86400),max_age_by_kind=c.get('max_age_by_kind',{}))
    if set(raw['histories'])!=set(ETF_UNIVERSE):raise ValueError('All five ETF histories required')
    histories={s:pd.read_parquet(local(name)) for s,name in raw['histories'].items()}
    quotes={}
    for symbol,q in raw.get('quotes',{}).items():
        if symbol not in ETF_UNIVERSE:raise ValueError('Unknown quote symbol')
        envelope=QuoteEnvelope(symbol,q['feed'],q['received_at'],context.as_of.isoformat(),q['raw_payload'])
        quotes[symbol]=validate_quote(envelope)
    risk_source=local(raw['risk_state'])
    positions=raw.get('positions',[])
    if not isinstance(positions,list):raise ValueError('Positions must be a list')
    for position in positions:
        if position.get('symbol') not in ETF_UNIVERSE:raise ValueError('Position outside frozen universe')
        if any(not math.isfinite(float(position[k])) or float(position[k])<=0 for k in ['quantity','price','stop_price']):raise ValueError('Invalid position values')
        if float(position['stop_price'])>=float(position['price']) or float(position['quantity'])%1:raise ValueError('Invalid long whole-share exposure')
    with TemporaryDirectory(prefix='trader2-shadow-risk-') as temp:
        path=Path(temp)/'risk.json';path.write_bytes(risk_source.read_bytes());risk=AccountRisk(path)
        if risk.state is not None:
            # Re-latch existing drawdown/daily breaches; never reset a session or highwater.
            risk.observe(risk.state['equity'],risk.state['session'],risk.state['cashflow_total'])
        result=run_shadow(context=context,histories=histories,quotes=quotes,spec=spec,risk=risk,
            positions=positions,cash=raw['cash'],output_dir=output,session_open=raw['session_open'],
            session_close=raw['session_close'],previous_session_close=raw.get('previous_session_close'),
            history_available_at=raw.get('history_available_at'))
    report=asdict(result['team']);report.update(method='deterministic_evidence_analysis',provider_configured=False)
    report['evidence_sources']={e.source_id:dict(source_url=e.source_url,kind=e.kind,observed_at=e.observed_at.isoformat(),published_at=e.published_at.isoformat(),available_at=e.available_at.isoformat(),vintage_at=e.vintage_at.isoformat() if e.vintage_at else None) for e in evidence}
    write_json(Path(output)/'team_report.json',report)
    write_json(Path(output)/'inputs.json',dict(registry_hash=registry['registry_hash'],source_hashes=hashes,
        method='local_snapshot_diagnostic',formal_forward_run=False,broker_orders_submitted=0))
    artifact_index(output)
    return dict(mode='diagnostic_shadow',formal_forward_run=False,broker_orders_submitted=0,
        approved_for_trading=False,decisions=result['decisions'],output_dir=str(output))
