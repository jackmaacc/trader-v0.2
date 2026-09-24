"""10,000 distinct full-chronology ETF execution-sensitivity replays; no orders."""
from __future__ import annotations
import argparse, hashlib, json, shutil, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
import multiprocessing as mp
import numpy as np
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from trader_engine.research.strategy_spec import StrategySpec,CANDIDATE_IDS
from trader_engine.research.etf_sensitivity import prepare_ytd,compiled_library,run_scenario,DAILY_FIELDS
from trader_engine.workflows.net_edge import load_dataset
from trader_engine.backtest.engine import BacktestEngine

SEED=20260923
_REF_INPUTS=None

def digest(path):
    h=hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""): h.update(b)
    return h.hexdigest()

def write_json(path,value):
    Path(path).write_text(json.dumps(value,indent=2,allow_nan=False,
        default=lambda x:x.isoformat() if hasattr(x,"isoformat") else x.item() if isinstance(x,np.generic) else str(x)))

def scenarios(seed=SEED):
    rng=np.random.default_rng(seed);grid=[]
    for delay in range(4):
        costs=1+(np.arange(625)+rng.random(625))*27/625
        if delay==0: costs[0],costs[1]=7.,14.
        if delay==1: costs[0]=14.
        if delay==3: costs[-1]=28.
        for cost in costs: grid.append((float(cost),delay))
    assert len(set(grid))==2500
    rows=[]
    for candidate in CANDIDATE_IDS:
        for cost,delay in grid:
            spec=replace(StrategySpec(candidate),slippage_bps=cost)
            key=hashlib.sha256(json.dumps(dict(spec=asdict(spec),delay=delay),sort_keys=True).encode()).hexdigest()
            rows.append(dict(trial_id=len(rows),candidate_id=candidate,one_way_cost_bps=cost,delay_minutes=delay,
                scenario_hash=key,execution_spec_hash=spec.spec_hash))
    table=pd.DataFrame(rows)
    assert len(table)==10000 and table.scenario_hash.nunique()==10000
    assert (table.groupby(["candidate_id","delay_minutes"]).size()==625).all()
    return table

def reference_job(job):
    global _REF_INPUTS
    data_path,candidate,cost,delay,out_path,start,end=job
    if _REF_INPUTS is None: _REF_INPUTS=load_dataset(data_path)
    _,frames,daily,schedule,actions=_REF_INPUTS
    dates=schedule.market_open.dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
    schedule=schedule.loc[(dates>=start)&(dates<=end)]
    spec=replace(StrategySpec(candidate),slippage_bps=cost)
    t=time.perf_counter()
    result=BacktestEngine.run_etf_replay(frames,spec,schedule=schedule,
        daily_signal_frames=daily,corporate_actions=actions,execution_delay_minutes=delay)
    curve=result.equity_curve
    sessions=curve.timestamp.dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
    marks=curve.groupby(sessions,sort=True).tail(1).copy()
    marks.insert(0,"session",sessions.loc[marks.index])
    path=np.r_[100000.,curve.equity.to_numpy()]
    metrics=result.metrics|dict(max_close_mark_drawdown=float(np.max(1-path/np.maximum.accumulate(path))),
        daily_halt_days=int(marks.daily_halt.sum()))
    fills=result.decisions.loc[result.decisions.action.isin(["entry","exit"]),["timestamp","symbol","action","reason","quantity","price"]]
    out=Path(out_path);out.mkdir(parents=True)
    marks.to_parquet(out/"daily.parquet",index=False)
    fills.to_parquet(out/"fills.parquet",index=False)
    result.trades.to_parquet(out/"trades.parquet",index=False)
    write_json(out/"metrics.json",metrics)
    return dict(candidate=candidate,cost=cost,delay=delay,seconds=time.perf_counter()-t,path=str(out))

def compare_reference(prepared,spec,delay,library,root):
    fast=run_scenario(prepared,spec,delay,library=library)
    root=Path(root)
    slow=pd.read_parquet(root/"daily.parquet")
    metrics=json.loads((root/"metrics.json").read_text())
    fields=["equity","cash","cumulative_gross_pnl","cumulative_net_pnl","cumulative_fees",
        "cumulative_impact_cost","dividend_receivable","dividends_paid","open_positions","daily_halt","drawdown_halt"]
    assert list(fast.daily.session)==list(slow.session)
    np.testing.assert_allclose(fast.daily[fields].to_numpy(float),slow[fields].to_numpy(float),atol=1e-6,rtol=0)
    for key in ["final_equity","gross_pnl","net_pnl","fees","modeled_impact","dividend_receivable","dividends_paid",
                "trade_count","open_positions","drawdown_halt","daily_halt_days","max_close_mark_drawdown","data_complete","valuation_valid"]:
        np.testing.assert_allclose(float(fast.metrics[key]),float(metrics[key]),atol=1e-6,rtol=0,err_msg=key)
    fills=pd.read_parquet(root/"fills.parquet").reset_index(drop=True)
    actual=fast.events.reset_index(drop=True)
    for key in ["timestamp","symbol","action","reason"]:
        assert list(actual[key])==list(fills[key]),key
    np.testing.assert_array_equal(actual.quantity,fills.quantity)
    np.testing.assert_allclose(actual.price,fills.price,atol=1e-10,rtol=0)
    trades=pd.read_parquet(root/"trades.parquet")
    exits=actual.loc[actual.action=="exit"]
    assert len(exits)==len(trades)
    if len(exits):
        np.testing.assert_allclose(exits.episode_net,trades.pnl,atol=1e-6,rtol=0)
        np.testing.assert_allclose(exits.episode_gross,trades.gross_pnl,atol=1e-6,rtol=0)
        closed=pd.to_datetime(trades.exit_time,utc=True).dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d").value_counts()
        expected=np.cumsum([closed.get(day,0) for day in prepared.sessions])
    else: expected=np.zeros(len(prepared.sessions))
    np.testing.assert_array_equal(fast.daily.trade_count,expected)
    error=float(np.max(np.abs(fast.daily[fields].to_numpy(float)-slow[fields].to_numpy(float))))
    return dict(candidate_id=spec.candidate_id,cost_bps=spec.slippage_bps,delay_minutes=delay,passed=True,
        maximum_daily_currency_difference=error,fills=len(fills),matched_sessions=len(slow))

def source_hashes(root):
    files=list((root/"src").rglob("*.py"))+list((root/"src").rglob("*.cpp"))
    files+=[Path(__file__),root/"scripts/prepare_etf_ytd_inputs.py"]
    return {str(p.relative_to(root)):digest(p) for p in sorted(files)}

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs",required=True,help="Prepared bundle containing dataset, raw, and refresh audit")
    parser.add_argument("--output",required=True)
    parser.add_argument("--workers",type=int,default=6)
    parser.add_argument("--reference-workers",type=int,default=4)
    parser.add_argument("--project-root",default=str(Path(__file__).resolve().parents[1]))
    args=parser.parse_args(argv)
    if not 1<=args.workers<=8 or not 1<=args.reference_workers<=8: raise ValueError("Use 1-8 workers")
    source=Path(__file__).resolve().parents[1];output=Path(args.output)
    if output.exists(): raise FileExistsError("Choose a new immutable output directory")
    inputs=Path(args.inputs)
    manifest,frames,daily,schedule,actions=load_dataset(inputs/"dataset")
    start,end="2026-01-01","2026-09-23"
    dates=schedule.market_open.dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
    selected=schedule.loc[(dates>=start)&(dates<=end)]
    sessions=tuple(selected.market_open.dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d"))
    if not sessions or sessions[-1]!=end: raise ValueError("Calendar must include the completed September 23 session")
    output.mkdir(parents=True)
    shutil.copytree(inputs,output/"inputs")
    snapshot=output/"source_snapshot"
    shutil.copytree(source/"src",snapshot/"src",ignore=shutil.ignore_patterns("__pycache__","*.pyc","*.DS_Store"))
    (snapshot/"scripts").mkdir()
    for p in [Path(__file__),source/"scripts/prepare_etf_ytd_inputs.py"]: shutil.copy2(p,snapshot/"scripts"/p.name)
    shutil.copy2(source/"requirements.txt",snapshot/"requirements.txt")
    grid=scenarios();grid.to_csv(output/"scenarios.csv",index=False)
    baseline_indices=[0,1,625,1250,1600,2499]
    checks=grid.iloc[[2500*c+i for c in range(4) for i in baseline_indices]]
    git_sha=subprocess.check_output(["git","-C",args.project_root,"rev-parse","HEAD"],text=True).strip()
    frozen_sources=source_hashes(source)
    protocol=dict(created_at=datetime.now(timezone.utc).isoformat(),seed=SEED,simulations=10000,
        unique_scenarios=10000,per_candidate=2500,candidates=list(CANDIDATE_IDS),universe=["SPY","QQQ","IWM","TLT","GLD"],
        session_count=len(sessions),sessions=sessions,start_flat_equity=100000.,continuous=True,
        actual_chronology=True,resampled=False,parameter_tuning=False,delay_minutes=[0,1,2,3],
        cost_range_bps_per_side=[1,28],grid="625 stratified costs per delay; same grid across four candidates; explicit 7/14/28bps anchors",
        cost_assumptions="Cost bps entered as modeled one-way slippage; commissions/spread zero to avoid double count; uncalibrated sensitivity assumptions",
        delay_scope="Entry observations and scheduled MOM nonpositive exits; existing stop/target/deadline rules unchanged",
        initial_positions="flat; pre-2026 daily history is signal warmup only",overhead="unknown; business P&L not qualified",
        strategy_specs=[asdict(StrategySpec(c)) for c in CANDIDATE_IDS],
        base_git_commit=git_sha,source_hashes=frozen_sources,scenario_file_sha256=digest(output/"scenarios.csv"),
        input_manifest_sha256=digest(output/"inputs/dataset/manifest.json"),
        execution_correction="Already-marketable initial stop fills at min(open,stop), with matching initial risk sizing; signals unchanged",
        compiler=subprocess.check_output(["clang++","--version"],text=True).splitlines()[0],
        runtime_versions=dict(python=sys.version,numpy=np.__version__,pandas=pd.__version__),
        reference_check_count=len(checks),approved_for_trading=False,broker_orders_submitted=0)
    write_json(output/"protocol.json",protocol)
    library=compiled_library(output/"build")
    prepared={}
    for candidate in CANDIDATE_IDS:
        prepared[candidate]=prepare_ytd(frames,daily,schedule,actions,candidate,start,end)
        print("Prepared "+candidate+" for "+str(len(sessions))+" sessions",flush=True)
    verification=[]
    jobs=[]
    for row in checks.itertuples():
        jobs.append((str(output/"inputs/dataset"),row.candidate_id,row.one_way_cost_bps,int(row.delay_minutes),
            str(output/"reference_checks"/str(row.trial_id)),start,end))
    with ProcessPoolExecutor(max_workers=args.reference_workers,mp_context=mp.get_context("spawn")) as pool:
        futures=[pool.submit(reference_job,job) for job in jobs]
        for future in as_completed(futures):
            ref=future.result()
            spec=replace(StrategySpec(ref["candidate"]),slippage_bps=ref["cost"])
            check=compare_reference(prepared[ref["candidate"]],spec,ref["delay"],library,ref["path"])
            verification.append(check)
            print("Full-YTD reference parity "+str(len(verification))+"/"+str(len(checks))+" "+ref["candidate"],flush=True)
    write_json(output/"reference_verification.json",dict(checks=verification,passed=True,tolerance_dollars=1e-6))
    print("All historical reference checks passed; starting 10,000 complete chronological replays",flush=True)
    values=np.lib.format.open_memmap(output/"daily_values.npy",mode="w+",dtype=np.float64,shape=(10000,len(sessions),len(DAILY_FIELDS)))
    (output/"fills").mkdir()
    results=[None]*10000;pending_events=[];part=0;completed=0;began=time.perf_counter()
    def job(row):
        spec=replace(StrategySpec(row.candidate_id),slippage_bps=float(row.one_way_cost_bps))
        return row,run_scenario(prepared[row.candidate_id],spec,int(row.delay_minutes),library=library)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        # Bounded batches keep result/trace memory independent of total trial count.
        for base in range(0,10000,200):
            futures=[pool.submit(job,row) for row in grid.iloc[base:base+200].itertuples(index=False)]
            for future in as_completed(futures):
                row,result=future.result();trial=int(row.trial_id)
                if tuple(result.daily.session)!=sessions: raise AssertionError("A replay dropped or changed session dates")
                matrix=result.daily[list(DAILY_FIELDS)].to_numpy(float)
                if not np.isfinite(matrix).all(): raise AssertionError("Nonfinite replay ledger")
                np.testing.assert_allclose(matrix[-1,0],100000+result.metrics["net_pnl"],atol=1e-6,rtol=0)
                values[trial]=matrix
                event_numeric=result.events[["time_index","symbol_index","side_code","reason_code","quantity","price"]].to_numpy(float)
                fingerprint=hashlib.sha256(np.round(matrix,8).tobytes()+np.round(event_numeric,8).tobytes()).hexdigest()
                turnover=float((result.events.quantity*result.events.price).sum()/100000)
                metrics=result.metrics|dict(trial_id=trial,candidate_id=row.candidate_id,one_way_cost_bps=row.one_way_cost_bps,
                    delay_minutes=int(row.delay_minutes),scenario_hash=row.scenario_hash,outcome_hash=fingerprint,turnover=turnover)
                results[trial]=metrics
                if not result.events.empty:
                    ev=result.events.copy();ev.insert(0,"trial_id",trial);pending_events.append(ev)
                completed+=1
            if pending_events:
                pd.concat(pending_events,ignore_index=True).sort_values(["trial_id","time_index"],kind="stable").to_parquet(output/"fills"/f"part-{part:04d}.parquet",index=False)
                pending_events=[];part+=1
            values.flush()
            progress=dict(completed=completed,total=10000,elapsed_seconds=time.perf_counter()-began)
            write_json(output/"progress.tmp.json",progress);(output/"progress.tmp.json").replace(output/"progress.json")
            print(json.dumps(progress),flush=True)
    if source_hashes(source)!=frozen_sources: raise RuntimeError("Source changed during frozen execution")
    if len([r for r in results if r is not None])!=10000: raise AssertionError("Incomplete run")
    table=pd.DataFrame(results).sort_values("trial_id")
    if table.scenario_hash.nunique()!=10000 or table.trial_id.nunique()!=10000: raise AssertionError("Duplicate scenario identities")
    table.to_csv(output/"all_10000_results.csv",index=False)
    table.to_parquet(output/"all_10000_results.parquet",index=False)
    np.savez_compressed(output/"daily_paths.npz",values=values,fields=np.array(DAILY_FIELDS),
        dates=np.array(sessions),trial_ids=table.trial_id.to_numpy())
    del values
    (output/"daily_values.npy").unlink()
    summaries=[]
    for candidate,g in table.groupby("candidate_id",sort=False):
        summaries.append(dict(candidate_id=candidate,runs=len(g),profitable=int((g.net_pnl>1e-6).sum()),
            losing=int((g.net_pnl< -1e-6).sum()),inactive=int((g.entry_count==0).sum()),
            minimum_net_pnl=float(g.net_pnl.min()),median_net_pnl=float(g.net_pnl.median()),maximum_net_pnl=float(g.net_pnl.max()),
            drawdown_halts=int(g.drawdown_halt.sum()),maximum_close_drawdown=float(g.max_close_mark_drawdown.max()),
            unique_outcomes=int(g.outcome_hash.nunique())))
    anchors=table.loc[((table.one_way_cost_bps==7)&(table.delay_minutes==0))|((table.one_way_cost_bps==14)&table.delay_minutes.isin([0,1]))]
    anchors.to_csv(output/"baseline_and_stress.csv",index=False)
    table.sort_values("net_pnl",ascending=False).head(20).to_csv(output/"exploratory_top20_NOT_SELECTED.csv",index=False)
    grouped=table.assign(cost_band=pd.cut(table.one_way_cost_bps,[0,7,14,28],labels=["1-7","7-14","14-28"]))
    grouped.groupby(["candidate_id","delay_minutes","cost_band"],observed=True).agg(
        runs=("trial_id","size"),median_net_pnl=("net_pnl","median"),minimum_net_pnl=("net_pnl","min"),
        maximum_net_pnl=("net_pnl","max"),profitable=("net_pnl",lambda x:int((x>1e-6).sum()))
    ).reset_index().to_csv(output/"sensitivity_by_cost_and_delay.csv",index=False)
    stats=dict(completed=10000,unique_configurations=int(table.scenario_hash.nunique()),unique_outcomes=int(table.outcome_hash.nunique()),
        sessions_per_run=len(sessions),total_simulated_sessions=10000*len(sessions),
        full_chronology=True,resampled=False,reference_checks_passed=len(verification),
        maximum_reference_daily_difference=max(x["maximum_daily_currency_difference"] for x in verification),
        candidates=summaries,elapsed_scenario_seconds=time.perf_counter()-began,
        profitable_runs=int((table.net_pnl>1e-6).sum()),losing_runs=int((table.net_pnl< -1e-6).sum()),
        approved_for_trading=False,broker_orders_submitted=0,business_pnl_qualified=False)
    write_json(output/"summary.json",stats)
    lines=["# 10,000 chronological YTD execution simulations","",
        f"January 2–September 23, 2026: {len(sessions)} actual exchange sessions in every run. $100,000 initial equity, flat start; no resets within a run.",
        "","## What ran","",
        "Four frozen ETF candidates, 2,500 unique execution configurations each. Costs span 1–28 basis points per side and entry delays 0–3 minutes. Each cost/delay configuration is replayed on the original minute observations in original date order. There is no bootstrap, shuffled history, strategy-parameter optimization, leverage, shorting, or automatic promotion.",
        "","The same execution grid is applied to every candidate. Signals come from the shared Python strategy functions. The compiled ledger was compared against the existing event engine across 24 full-YTD scenarios before the 10,000-run batch.",
        "","## Results","",
        "| Candidate | Profitable / 2,500 | Median net P&L | Best net P&L | Worst net P&L | Drawdown halts |",
        "|---|---:|---:|---:|---:|---:|"]
    for g in summaries:
        lines.append("| {} | {} | ${:,.2f} | ${:,.2f} | ${:,.2f} | {} |".format(g["candidate_id"],g["profitable"],g["median_net_pnl"],g["maximum_net_pnl"],g["minimum_net_pnl"],g["drawdown_halts"]))
    lines+=["","These are net trading results after the modeled execution costs, before unknown recurring operating expenses. Final holdings and unpaid distributions remain marked; no artificial terminal liquidation is added.",
        "",f"Distinct configurations: {stats['unique_configurations']:,}. Distinct rounded daily/fill outcomes: {stats['unique_outcomes']:,}. Identical outcomes can occur when different rules/assumptions produce identical realized trades.",
        "","The profitable fraction describes this deliberately chosen sensitivity grid. It is **not a probability of making money**. Shared historical prices make the trials highly dependent; the best historical result is not a validated selection.",
        "","## Data and execution corrections","",
        "Alpaca historical SIP minute bars, adjusted daily signal history, exchange calendar and corporate actions are archived with hashes. Two missing September ex-date distributions (SPY and QQQ) were recovered by querying later processing dates and filtering by already elapsed ex-dates. October payments remain receivables at the September 23 endpoint.",
        "","Five TLT minute observations remain absent after direct provider rechecks; they are not fabricated. All 182 calendar sessions remain in every replay. Missingness and stale held-position marks are disclosed in each scenario's metrics and block qualification.",
        "","A newly placed stop already beyond the observed open now fills at min(open, stop), with matching entry risk sizing. This corrects an optimistic execution assumption without changing signals. Historical publication/receipt times and exact intraday dividend credit time remain unverified; pay-date 09:30 ET is a documented diagnostic convention.",
        "","## Interpretation and limits","",
        "Daily 0.5% and portfolio 3% drawdown triggers retain the current model's action semantics, including overnight gaps and permanent drawdown halt. Halted scenarios still include subsequent cash days. Action thresholds are not hard loss guarantees. Costs and delays are uncalibrated assumptions; stop/target fills retain minute-OHLC ordering assumptions.",
        "","This is retrospective development evidence, including previously examined dates. Unknown overhead, incomplete minute coverage, unverified live costs and remaining execution-readiness gates prevent promotion. No 126-session prospective trial or live trading approval is produced.",
        "","## Evidence files","",
        "- protocol.json and scenarios.csv: frozen rules, exact 10,000 configurations, seed, code/data hashes and dates.",
        "- all_10000_results.csv/parquet: every scenario, including losses, inactivity and halted runs.",
        "- daily_paths.npz: all 1,820,000 dated daily ledger records, field names and trial IDs.",
        "- fills/: all simulated entry and exit events, quantities, prices, costs and episode P&L.",
        "- reference_checks/ and reference_verification.json: all 24 full-history comparisons.",
        "- baseline_and_stress.csv and sensitivity_by_cost_and_delay.csv: matched execution comparisons.",
        "- inputs/: original provider archives, corrected dataset and refresh audit.",
        "- source_snapshot/: exact research code; build/: compiled kernel for this machine.",
        "- artifact_index.json: complete output-file hashes.",
        "","No broker orders were submitted. No candidate was selected from these runs."]
    (output/"REPORT.md").write_text("\n".join(lines)+"\n")
    index={str(p.relative_to(output)):digest(p) for p in sorted(output.rglob("*")) if p.is_file() and p.name!="artifact_index.json"}
    write_json(output/"artifact_index.json",dict(files=index,complete=True,approved_for_trading=False,broker_orders_submitted=0))
    print(json.dumps(stats),flush=True)
    return stats

if __name__=="__main__": main()
