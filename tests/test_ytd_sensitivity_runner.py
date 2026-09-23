import importlib.util
import json
from pathlib import Path
import pandas as pd
import pytest

def module(name):
    path=Path(__file__).parents[1]/"scripts"/(name+".py")
    spec=importlib.util.spec_from_file_location(name,path)
    obj=importlib.util.module_from_spec(spec);spec.loader.exec_module(obj)
    return obj

def test_ten_thousand_distinct_fixed_rule_execution_scenarios():
    runner=module("run_etf_ytd_10000")
    a=runner.scenarios();b=runner.scenarios()
    pd.testing.assert_frame_equal(a,b)
    assert len(a)==10000 and a.scenario_hash.nunique()==10000
    assert a.groupby(["candidate_id","delay_minutes"]).size().eq(625).all()
    assert a.one_way_cost_bps.between(1,28).all()
    grids=[g[["one_way_cost_bps","delay_minutes"]].reset_index(drop=True) for _,g in a.groupby("candidate_id")]
    for other in grids[1:]:pd.testing.assert_frame_equal(grids[0],other)
    assert len(a.loc[(a.one_way_cost_bps==7)&(a.delay_minutes==0)])==4
    assert not a.scenario_hash.equals(runner.scenarios(20260924).scenario_hash)

def test_action_refresh_includes_elapsed_ex_date_and_retains_real_gap(tmp_path):
    prep=module("prepare_etf_ytd_inputs")
    source=tmp_path/"original";source.mkdir()
    (source/"minutes").mkdir();(source/"daily").mkdir()
    raw=tmp_path/"raw";raw.mkdir()
    times=pd.date_range("2026-09-23 13:30",periods=2,freq="min",tz="UTC")
    for symbol in ["SPY","QQQ","IWM","TLT","GLD"]:
        f=pd.DataFrame(dict(open=100.,high=101.,low=99.,close=100.,volume=100.),index=times)
        if symbol=="TLT":f=f.iloc[:1]
        f.to_parquet(source/"minutes"/(symbol+".parquet"))
        daily=pd.DataFrame(dict(high=[101.],low=[99.],close=[100.],total_return_close=[100.],signal_scale=[1.]),index=pd.DatetimeIndex(["2026-09-22"],tz="America/New_York"))
        daily.to_parquet(source/"daily"/(symbol+".parquet"))
    schedule=pd.DataFrame(dict(market_open=[times[0]],market_close=[times[-1]+pd.Timedelta(minutes=1)]))
    schedule.to_parquet(source/"schedule.parquet",index=False)
    actions=pd.DataFrame(columns=["symbol","split_ratio","cash_dividend","payment_timestamp","source_id"],index=pd.DatetimeIndex([],tz="UTC",name="timestamp"))
    actions.to_parquet(source/"actions.parquet")
    (source/"coverage.json").write_text("[]")
    manifest=dict(hashes={str(p.relative_to(source)):prep.digest(p) for p in source.rglob("*") if p.is_file()})
    (source/"manifest.json").write_text(json.dumps(manifest))
    refresh=tmp_path/"refresh.json"
    refresh.write_text(json.dumps(dict(response=dict(structuredContent=dict(data=dict(corporate_actions=dict(cash_dividends=[
        dict(id="elapsed",symbol="SPY",ex_date="2026-09-23",rate=1.,payable_date="2026-10-01"),
        dict(id="future",symbol="SPY",ex_date="2026-09-24",rate=1.,payable_date="2026-10-02")
    ]),next_page_token=None))))))
    recheck=tmp_path/"recheck.json"
    bars={"bars":{"TLT":[{"t":"2026-09-23T13:32:00Z","o":100,"h":101,"l":99,"c":100,"v":20}]}}
    response={"structuredContent":{"data":bars}}
    record={"request":{"symbols":"TLT","start":times[-1].isoformat()},"response":response}
    recheck.write_text(json.dumps([{"status":"fulfilled","value":record}]))
    before=prep.digest(source/"actions.parquet")
    result=prep.prepare(source,raw,refresh,recheck,tmp_path/"new")
    out=pd.read_parquet(result/"actions.parquet")
    assert list(out.source_id)==["elapsed"]
    assert out.payment_timestamp.iloc[0]>times[-1]
    assert len(pd.read_parquet(result/"minutes/TLT.parquet"))==1
    assert prep.digest(source/"actions.parquet")==before
    audit=json.loads((result.parent/"data_refresh_audit.json").read_text())
    assert audit["ignored_future_ex_date_ids"]==["future"]
    assert audit["missing_bar_rechecks"][0]["status"]=="provider_still_missing"
    with pytest.raises(FileExistsError):prep.prepare(source,raw,refresh,recheck,tmp_path/"new")
