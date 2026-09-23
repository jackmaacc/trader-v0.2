"""Prepare an immutable YTD input bundle from archived connected Alpaca responses."""
from __future__ import annotations
import argparse, hashlib, json, shutil
from pathlib import Path
import pandas as pd

def digest(path):
    h=hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""): h.update(b)
    return h.hexdigest()

def unwrap(record):
    response=record.get("response",record)
    if response.get("isError"): raise ValueError("Provider returned an error")
    data=response.get("structuredContent",{}).get("data")
    if data is None:
        block=next(b for b in response["content"] if b["type"]=="text")
        data=json.loads(block["text"]);data=data.get("data",data)
    if isinstance(data,dict) and data.get("error"): raise ValueError("Nested provider error")
    return data

def prepare(source,raw_source,refresh,recheck,output,end="2026-09-23"):
    source,raw_source,output=map(Path,(source,raw_source,output))
    original=json.loads((source/"manifest.json").read_text())
    for name,value in original["hashes"].items():
        if Path(name).is_absolute() or ".." in Path(name).parts: raise ValueError("Unsafe source manifest")
        if digest(source/name)!=value: raise ValueError("Source integrity failure")
    if output.exists(): raise FileExistsError("New input bundle required")
    output.mkdir(parents=True)
    data=output/"dataset";shutil.copytree(source,data)
    shutil.copytree(raw_source,output/"raw")
    shutil.copy2(refresh,output/"raw/actions_refresh.json")
    shutil.copy2(recheck,output/"raw/missing_bars_recheck.json")
    actions=pd.read_parquet(data/"actions.parquet")
    schedule=pd.read_parquet(data/"schedule.parquet")
    dates=schedule.market_open.dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
    opens=dict(zip(dates,schedule.market_open))
    payload=unwrap(json.loads(Path(refresh).read_text()))
    if payload.get("next_page_token"): raise ValueError("Incomplete action pagination")
    groups=payload.get("corporate_actions",{})
    if set(groups)-{"cash_dividends"}: raise ValueError("Refresh requires review of non-dividend actions")
    ids=set(actions.source_id)
    additions=[];ignored=[]
    for row in groups.get("cash_dividends",[]):
        day=row.get("ex_date")
        if not day: raise ValueError("Unknown ex-date")
        if day>end: ignored.append(row["id"]);continue
        if day not in opens: raise ValueError("Action outside supplied calendar")
        payment=pd.Timestamp(row["payable_date"]+" 09:30",tz="America/New_York").tz_convert("UTC") if row.get("payable_date") else pd.NaT
        event=dict(symbol=row["symbol"],split_ratio=1.,cash_dividend=float(row["rate"]),
                   payment_timestamp=payment,source_id=row["id"])
        if row["id"] in ids:
            previous=actions.loc[actions.source_id==row["id"]]
            if len(previous)!=1 or previous.index[0]!=opens[day] or previous.iloc[0].symbol!=row["symbol"] or abs(previous.iloc[0].cash_dividend-event["cash_dividend"])>1e-12:
                raise ValueError("Changed historical action requires explicit revision handling")
            continue
        additions.append((opens[day],event));ids.add(row["id"])
    if additions:
        extra=pd.DataFrame([r for _,r in additions],index=pd.DatetimeIndex([t for t,_ in additions],name=actions.index.name))
        actions=pd.concat([actions,extra]).sort_index(kind="stable")
    actions.to_parquet(data/"actions.parquet")
    rechecks=[]
    for item in json.loads(Path(recheck).read_text()):
        if item["status"]!="fulfilled": raise ValueError("Missing-bar recheck failed")
        record=item["value"];request=record["request"];symbol=request["symbols"];target=pd.Timestamp(request["start"])
        rows=unwrap(record).get("bars",{}).get(symbol,[])
        exact=[r for r in rows if pd.Timestamp(r["t"])==target]
        if exact:
            frame=pd.read_parquet(data/"minutes"/(symbol+".parquet"))
            if target in frame.index: raise ValueError("Requested gap was already populated")
            r=exact[0]
            frame.loc[target,["open","high","low","close","volume"]]=[r[k] for k in ["o","h","l","c","v"]]
            frame.sort_index().to_parquet(data/"minutes"/(symbol+".parquet"))
        rechecks.append(dict(symbol=symbol,timestamp=target.isoformat(),status="recovered" if exact else "provider_still_missing"))
    # Compare adjustment discontinuities with actual corporate-action records.
    uncovered=[]
    for symbol in ["SPY","QQQ","IWM","TLT","GLD"]:
        f=pd.read_parquet(data/"daily"/(symbol+".parquet"))
        raw=f.close*f.signal_scale
        implied=raw.shift()*(1-f.signal_scale/f.signal_scale.shift())
        for t,value in implied.loc[(implied.index.year==2026)&(implied.index.strftime("%Y-%m-%d")<=end)&(implied.abs()>.01)].items():
            matching=actions.loc[(actions.symbol==symbol)&(actions.index.tz_convert("America/New_York").date==t.date())]
            if matching.empty: uncovered.append(dict(symbol=symbol,date=str(t.date()),implied_adjustment=float(value)))
    if uncovered: raise ValueError("Unexplained adjustment discontinuities: "+str(uncovered))
    # Recompute coverage without deleting missing observations or sessions.
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
    from trader_engine.data.evidence import coverage_audit
    calendar=[dict(date=day,open=op.isoformat(),close=cl.isoformat()) for day,op,cl in zip(dates,schedule.market_open,schedule.market_close)]
    frames={s:pd.read_parquet(data/"minutes"/(s+".parquet")) for s in ["SPY","QQQ","IWM","TLT","GLD"]}
    coverage=coverage_audit(frames,calendar)
    (data/"coverage.json").write_text(json.dumps(coverage,indent=2))
    audit=dict(added_actions=[dict(ex_date=t.isoformat(),**{k:str(v) if isinstance(v,pd.Timestamp) else v for k,v in r.items()}) for t,r in additions],
        ignored_future_ex_date_ids=ignored,missing_bar_rechecks=rechecks,unexplained_adjustments=uncovered,
        processing_date_filter="Queried through Dec 31 to include known ex-dates with later payment/processing dates; future ex-dates excluded",
        payment_timing="Documented payable date at 09:30 ET diagnostic convention; exact broker credit time unverified",
        original_publication_times_known=False)
    (output/"data_refresh_audit.json").write_text(json.dumps(audit,indent=2))
    manifest=original|dict(parent_manifest_sha256=digest(source/"manifest.json"),
        actions_refresh_sha256=digest(refresh),missing_bar_recheck_sha256=digest(recheck),
        source_archive="../raw",coverage_complete=all(r["complete"] for r in coverage),
        corporate_actions_verified=True,corporate_action_publication_times_known=False,
        execution_cost_calibration_verified=False,historical_role="development_only")
    manifest["hashes"]={str(p.relative_to(data)):digest(p) for p in data.rglob("*") if p.is_file() and p.name!="manifest.json"}
    manifest["files"]=manifest["hashes"]
    (data/"manifest.json").write_text(json.dumps(manifest,indent=2))
    print(json.dumps(audit,indent=2))
    return data

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    for flag in ["source","raw-source","refresh","recheck","output"]:p.add_argument("--"+flag,required=True)
    a=p.parse_args();prepare(a.source,a.raw_source,a.refresh,a.recheck,a.output)
