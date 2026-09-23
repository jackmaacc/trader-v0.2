"""Normalize archived connected-Alpaca responses without credentials or network."""
from __future__ import annotations
import argparse,gzip,hashlib,json,sys
from pathlib import Path
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from trader_engine.data.evidence import parse_calendar,coverage_audit
from trader_engine.research.strategy_spec import ETF_UNIVERSE

def read(path):
    with gzip.open(path,"rt") as f: obj=json.load(f)
    response=obj.get("response",obj)
    d=response.get("structuredContent",{}).get("data")
    if d is None:
        for block in response.get("content",[]):
            if block.get("type")=="text":
                try:
                    value=json.loads(block["text"]);d=value.get("data",value);break
                except ValueError:pass
    if d is None or response.get("isError") or isinstance(d,dict) and d.get("error"):
        raise ValueError("Unsuccessful source response: "+str(path))
    return d

def bars(payload,symbol):
    rows=payload.get("bars",{}).get(symbol,[])
    f=pd.DataFrame(rows)
    if f.empty:return pd.DataFrame(columns=["open","high","low","close","volume"],index=pd.DatetimeIndex([],tz="UTC"))
    f.index=pd.to_datetime(f.pop("t"),utc=True)
    f=f.rename(columns={"o":"open","h":"high","l":"low","c":"close","v":"volume"})
    return f[["open","high","low","close","volume"]].astype(float).sort_index()

def digest(path):
    h=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""):h.update(chunk)
    return h.hexdigest()

def normalize(source,output):
    source=Path(source);output=Path(output);output.mkdir(parents=True,exist_ok=False)
    (output/"minutes").mkdir();(output/"daily").mkdir()
    calendar_payload=read(source/"calendar.json.gz")
    if isinstance(calendar_payload,dict):calendar_payload=calendar_payload.get("calendar",calendar_payload.get("result",calendar_payload.get("data")))
    calendar=parse_calendar(calendar_payload)
    full=pd.DataFrame(calendar)
    schedule=pd.DataFrame({"market_open":pd.to_datetime(full.open,utc=True),"market_close":pd.to_datetime(full.close,utc=True)})
    schedule["previous_session"]=full.date.shift()
    schedule=schedule.loc[full.date>="2024-01-02"].reset_index(drop=True)
    schedule.to_parquet(output/"schedule.parquet",index=False)
    hours={r["date"]:(pd.Timestamp(r["open"]),pd.Timestamp(r["close"])) for r in calendar if r["date"]>="2024-01-02"}
    expected_minutes=pd.DatetimeIndex([t for opening,closing in hours.values() for t in pd.date_range(opening,closing,freq="min",inclusive="left")])
    issues=[];counts={};coverage=[];source_hashes={}
    for symbol in ETF_UNIVERSE:
        chunks=[]
        for path in sorted((source/symbol).glob("minute_*.json.gz")):
            source_hashes[str(path.relative_to(source))]=digest(path)
            try:f=bars(read(path),symbol)
            except ValueError:
                issues.append(str(path.relative_to(source))+": response failed");continue
            chunks.append(f)
        if not chunks:raise ValueError("No minute data for "+symbol)
        frame=pd.concat(chunks).sort_index()
        frame=frame.loc[frame.index.isin(expected_minutes)]
        if frame.index.has_duplicates:
            duplicate=frame.loc[frame.index.duplicated(keep=False)]
            if (duplicate.groupby(level=0).nunique()>1).any().any():raise ValueError("Conflicting duplicate bars")
            frame=frame.loc[~frame.index.duplicated()]
        frame.to_parquet(output/"minutes"/(symbol+".parquet"))
        raw_path=source/symbol/"daily_raw.json.gz";all_path=source/symbol/"daily_all.json.gz"
        raw=bars(read(raw_path),symbol);adjusted=bars(read(all_path),symbol)
        raw.index=raw.index.tz_convert("America/New_York").normalize()
        adjusted.index=adjusted.index.tz_convert("America/New_York").normalize()
        if not raw.index.equals(adjusted.index):raise ValueError("Raw/adjusted daily coverage differs")
        daily=adjusted.copy();daily["total_return_close"]=adjusted.close
        daily["signal_scale"]=raw.close/adjusted.close
        daily.to_parquet(output/"daily"/(symbol+".parquet"))
        for path in (raw_path,all_path):source_hashes[str(path.relative_to(source))]=digest(path)
        c=coverage_audit({symbol:frame},[r for r in calendar if r["date"]>="2024-01-02"])
        print("Normalized "+symbol,flush=True)
        coverage.extend(c);counts[symbol]={"minutes":len(frame),"daily":len(daily),"missing_minutes":sum(len(r["missing"]) for r in c)}
    actions=[];verified=False;action_file=source/"actions.json.gz"
    if action_file.exists():
        payload=read(action_file);groups=payload.get("corporate_actions",{})
        supported={"cash_dividends","forward_splits","reverse_splits"}
        verified=not payload.get("next_page_token") and not (set(groups)-supported)
        ids=set()
        for kind,rows in groups.items():
            for r in rows:
                if r["id"] in ids:raise ValueError("Duplicate action identity")
                ids.add(r["id"]);day=r.get("ex_date")
                if day is None:verified=False;continue
                if day<"2024-01-02":continue
                if r["symbol"] not in ETF_UNIVERSE:raise ValueError("Action outside universe")
                if day not in hours:verified=False;continue
                row={"timestamp":hours[day][0],"symbol":r["symbol"],"split_ratio":1.,"cash_dividend":0.,"payment_timestamp":pd.NaT,"source_id":r["id"]}
                if kind=="cash_dividends":row["cash_dividend"]=float(r["rate"])
                elif kind in {"forward_splits","reverse_splits"}:
                    try:row["split_ratio"]=float(r["new_rate"])/float(r["old_rate"])
                    except (KeyError,ValueError,ZeroDivisionError):verified=False;continue
                else:verified=False;continue
                # Missing payment dates stay unpaid receivables; never invent cash.
                payable=r.get("payable_date",r.get("pay_date"))
                if payable:row["payment_timestamp"]=pd.Timestamp(payable+" 09:30",tz="America/New_York").tz_convert("UTC")
                actions.append(row)
        source_hashes["actions.json.gz"]=digest(action_file)
    else:issues.append("Corporate action archive missing")
    af=pd.DataFrame(actions,columns=["timestamp","symbol","split_ratio","cash_dividend","payment_timestamp","source_id"])
    if af.empty:af=af.set_index(pd.DatetimeIndex([],tz="UTC")).drop(columns=["timestamp"])
    else:af=af.set_index("timestamp").sort_index()
    af.to_parquet(output/"actions.parquet")
    (output/"coverage.json").write_text(json.dumps(coverage,indent=2))
    source_hashes["calendar.json.gz"]=digest(source/"calendar.json.gz")
    manifest={"representation":"normalized_connected_Alpaca_archives","feed":"sip","minute_adjustment":"raw","daily_signal_adjustment":"all",
              "source_archive":"../raw","source_archive_capture_path":str(source.resolve()),"source_hashes":source_hashes,"counts":counts,"issues":issues,
              "corporate_actions_verified":verified,"corporate_action_publication_times_known":False,
              "unknown_payment_dates":"accrued receivables; not spendable cash",
              "cost_calibration_verified":False,"coverage_complete":all(r["complete"] for r in coverage),
              "historical_role":"development_only","files":{}}
    manifest["hashes"]={str(p.relative_to(output)):digest(p) for p in output.rglob("*") if p.is_file()}
    manifest["files"]=manifest["hashes"]
    (output/"manifest.json").write_text(json.dumps(manifest,indent=2))
    print(json.dumps({k:v for k,v in manifest.items() if k not in {"hashes","files","source_hashes"}},indent=2))
    return manifest

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("--input",required=True);p.add_argument("--output",required=True)
    args=p.parse_args();normalize(args.input,args.output)
