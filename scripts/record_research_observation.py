"""Record same-day after-close signals without placing orders or inventing paper fills."""
from __future__ import annotations
import argparse,json,hashlib,sys,os,tempfile
from pathlib import Path
from datetime import datetime,date,time
from zoneinfo import ZoneInfo
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from trader_engine.features.engine import FeatureEngineer
from trader_engine.core.config import FeatureConfig
from trader_engine.core.models import AssetClass
from trader_engine.data.base import validate_bars


def record_observation(plan_path: Path,bars_dir: Path,output_dir: Path,as_of: date,now=None):
    now=now or datetime.now(ZoneInfo('America/New_York'))
    if now.tzinfo is None:raise ValueError('Observation time requires a timezone')
    local=now.astimezone(ZoneInfo('America/New_York'))
    if as_of!=local.date() or local.time()<time(16,30) or local.weekday()>=5:
        raise ValueError('Only same-day weekday observations after 16:30 New York time are accepted')
    plan=json.loads(plan_path.read_text())
    if as_of<date.fromisoformat(plan['frozen_on']):raise ValueError('Cannot backdate observations before plan freeze')
    symbols=plan['symbols']
    if not symbols or len(symbols)!=len(set(symbols)):raise ValueError('Universe must be nonempty and unique')
    rows=[];fingerprints={}
    for symbol in symbols:
        if '/' in symbol or '\\' in symbol or symbol in {'.','..'}:raise ValueError('Invalid symbol path')
        path=bars_dir/f'{symbol}.parquet';raw=pd.read_parquet(path).loc[:as_of.isoformat()]
        if raw.empty or raw.index[-1].date()!=as_of:raise ValueError(f'Missing current session for {symbol}; observation not published')
        if raw.index.has_duplicates or not raw.index.is_monotonic_increasing:raise ValueError('Invalid bar order')
        validate_bars(raw,require_volume=True)
        f=FeatureEngineer(FeatureConfig()).transform(raw,AssetClass.EQUITY)
        row=f.iloc[-1];fast=f.close.rolling(50).mean().iloc[-1];slow=f.close.rolling(200).mean().iloc[-1]
        eligible=bool(pd.notna(fast) and pd.notna(slow) and pd.notna(row.atr_14) and row.atr_14>0 and fast>slow and row.close>slow and row.close>=3 and row.dollar_volume_20>=1000000)
        score=float((fast/slow-1)/(row.atr_14/row.close)) if eligible else None
        rows.append(dict(symbol=symbol,signal='long_candidate' if eligible else 'flat',score=score,close=float(row.close)))
        fingerprints[symbol]=hashlib.sha256(path.read_bytes()).hexdigest()
    ranked=sorted([r for r in rows if r['signal']=='long_candidate'],key=lambda r:(-r['score'],r['symbol']))
    payload=dict(as_of=as_of.isoformat(),recorded_at=now.isoformat(),mode='observation_only',orders_submitted=0,
        plan_sha256=hashlib.sha256(plan_path.read_bytes()).hexdigest(),price_file_sha256=fingerprints,
        signals=rows,top_candidates=[r['symbol'] for r in ranked[:10]],
        note='Candidates are not orders, fills, positions, or earned returns. No performance is credited until independently recorded future execution observations exist.')
    output_dir.mkdir(parents=True,exist_ok=True)
    path=output_dir/f'{as_of.isoformat()}.json'
    serialized=json.dumps(payload,indent=2,allow_nan=False)
    fd,temporary=tempfile.mkstemp(prefix='.observation-',dir=output_dir)
    try:
        with os.fdopen(fd,'w') as handle:
            handle.write(serialized);handle.flush();os.fsync(handle.fileno())
        os.link(temporary,path)  # Atomic publication; existing snapshots cannot be replaced.
    finally:
        os.unlink(temporary)
    return payload


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',type=Path,required=True);parser.add_argument('--bars-dir',type=Path,required=True)
    parser.add_argument('--output-dir',type=Path,required=True);parser.add_argument('--as-of',type=date.fromisoformat,required=True)
    args=parser.parse_args();result=record_observation(args.plan,args.bars_dir,args.output_dir,args.as_of)
    print(json.dumps({'as_of':result['as_of'],'observed_symbols':len(result['signals']),'orders_submitted':0,'mode':result['mode']}))

if __name__=='__main__':main()
