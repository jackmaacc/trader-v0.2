import importlib.util,json
from pathlib import Path
from datetime import datetime,date
from zoneinfo import ZoneInfo
import pandas as pd
import numpy as np
import pytest
spec=importlib.util.spec_from_file_location('observation',Path(__file__).resolve().parents[1]/'scripts/record_research_observation.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


def setup(tmp_path):
    plan=tmp_path/'plan.json';plan.write_text(json.dumps({'frozen_on':'2026-09-22','symbols':['ABC']}))
    index=pd.bdate_range(end='2026-09-22',periods=260);close=np.linspace(20,30,260)
    pd.DataFrame({'open':close,'high':close+1,'low':close-1,'close':close,'volume':100000},index=index).to_parquet(tmp_path/'ABC.parquet')
    return plan


def test_observation_is_immutable_and_has_no_orders(tmp_path):
    plan=setup(tmp_path);now=datetime(2026,9,22,18,tzinfo=ZoneInfo('America/New_York'))
    result=module.record_observation(plan,tmp_path,tmp_path/'observations',date(2026,9,22),now)
    assert result['orders_submitted']==0 and result['mode']=='observation_only'
    assert result['top_candidates']==['ABC']
    with pytest.raises(FileExistsError):module.record_observation(plan,tmp_path,tmp_path/'observations',date(2026,9,22),now)


@pytest.mark.parametrize('hour,day',[(12,22),(18,21)])
def test_backdating_and_preclose_recording_rejected(tmp_path,hour,day):
    plan=setup(tmp_path);now=datetime(2026,9,22,hour,tzinfo=ZoneInfo('America/New_York'))
    with pytest.raises(ValueError):module.record_observation(plan,tmp_path,tmp_path/'observations',date(2026,9,day),now)


def test_missing_current_bar_prevents_publication(tmp_path):
    plan=setup(tmp_path);now=datetime(2026,9,23,18,tzinfo=ZoneInfo('America/New_York'))
    with pytest.raises(ValueError,match='Missing current session'):module.record_observation(plan,tmp_path,tmp_path/'observations',date(2026,9,23),now)
    assert not (tmp_path/'observations').exists()
