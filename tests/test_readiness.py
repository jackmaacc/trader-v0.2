from datetime import date
import numpy as np
import pandas as pd
import pytest
from trader_engine.research.readiness import assess_prospective_record


def record():
    return pd.DataFrame({'session':pd.bdate_range('2026-01-02',periods=60),'equity':np.linspace(100100,105000,60),
        'stressed_equity':np.linspace(100050,103000,60),'benchmark_equity':np.linspace(100020,101000,60),'closed_trades':2,'reconciled':True})


def assess(frame,**kwargs):
    defaults=dict(frozen_on=date(2026,1,1),as_of=date(2026,9,22),independent_data_verified=True,execution_checks_passed=True,universe_scope_documented=True)
    return assess_prospective_record(frame,**(defaults|kwargs))


def test_valid_record_only_permits_review():
    result=assess(record());assert result['eligible_for_review'] and result['closed_trades']==120
    assert 'order authorization' in result['note']


def test_empty_record_cannot_pass():
    assert not assess(pd.DataFrame())['eligible_for_review']


@pytest.mark.parametrize('flag',['independent_data_verified','execution_checks_passed','universe_scope_documented'])
def test_unverified_dependencies_fail_closed(flag):
    assert not assess(record(),**{flag:False})['eligible_for_review']


def test_reused_and_future_sessions_fail():
    assert 'contains_pre_freeze_or_reused_sessions' in assess(record(),frozen_on=date(2026,9,22))['reasons']
    assert 'contains_future_sessions' in assess(record(),as_of=date(2026,1,3))['reasons']


def test_first_loss_counts_in_drawdown():
    f=record();f.loc[0,'equity']=90000
    result=assess(f);assert result['max_drawdown']>=.1-1e-10
    assert 'drawdown_limit_exceeded' in result['reasons']


def test_cost_stress_and_benchmark_failures_block_review():
    f=record();f.loc[59,'stressed_equity']=99000;f.loc[59,'benchmark_equity']=106000
    reasons=assess(f)['reasons'];assert 'nonpositive_stressed_return' in reasons and 'no_benchmark_excess' in reasons


@pytest.mark.parametrize('kind',['duplicate','nan','fractional_trade','false_string'])
def test_invalid_records_fail_closed(kind):
    f=record()
    if kind=='duplicate':f.loc[1,'session']=f.loc[0,'session']
    if kind=='nan':f.loc[1,'equity']=np.nan
    if kind=='fractional_trade':f['closed_trades']=.5
    if kind=='false_string':
        f['reconciled']='false';assert not assess(f)['eligible_for_review'];return
    with pytest.raises(ValueError):assess(f)
