import numpy as np
import pandas as pd
import pytest
from trader_engine.research.daily_hypotheses import (
    DailyFeature, HoldingState, daily_feature, decision_due, target_intents,
)
from trader_engine.research.strategy_spec import ETF_UNIVERSE


def history(n=230):
    dates=pd.bdate_range('2020-01-01',periods=n)
    c=100+np.arange(n)*.1+np.sin(np.arange(n))*.01
    return pd.DataFrame(dict(high=c+1,low=c-1,close=c,total_return_close=c,signal_scale=1.),index=dates)


def test_features_ignore_future_prices_and_require_real_contiguous_sessions():
    f=history();d=f.index[210]
    before=daily_feature('H1',f,f.index,d)
    altered=f.copy();altered.loc[altered.index>d,'total_return_close']=.01
    assert daily_feature('H1',altered,f.index,d)==before
    assert before.valid and before.trend
    assert not daily_feature('H1',f.drop(f.index[150]),f.index,d).valid


def test_rotation_skips_latest21_sessions_exactly():
    f=history(127)
    a=daily_feature('H2',f,f.index,f.index[-1])
    assert a.score==pytest.approx(f.total_return_close.iloc[-22]/f.total_return_close.iloc[-127]-1)
    f.iloc[-21:,f.columns.get_loc('total_return_close')]=1
    assert daily_feature('H2',f,f.index,f.index[-1]).score==a.score


def test_real_calendar_handles_holiday_shortened_week():
    assert decision_due('H2','2026-04-02','2026-04-06')
    assert not decision_due('H2','2026-04-01','2026-04-02')
    assert decision_due('H1','2026-04-30','2026-05-01')
    assert not decision_due('H1','2026-04-29','2026-04-30')


def features(**kw):
    return {s:DailyFeature(True,'valid',close=100,atr=1,**kw) for s in ETF_UNIVERSE}


def test_rotation_ties_retention_and_minimum_hold():
    f=features(score=.1)
    out=target_intents('H2',f,{})
    assert list(out)==['GLD','IWM']
    out=target_intents('H2',f,{'TLT':HoldingState(9,.05)})
    assert 'TLT' not in out and len(out)==1
    out=target_intents('H2',f,{'TLT':HoldingState(10,.05)})
    assert out['TLT'].target_weight==0 and len(out)==3


def test_cross_section_gap_skips_rotation_and_inverse_vol():
    f=features(volatility=.1);f['QQQ']=DailyFeature(False,'missing')
    assert target_intents('H2',f,{})=={}
    assert target_intents('H4',f,{})=={}


def test_breakout_minimum_maximum_and_cooldown():
    f=features(breakout=True,breakdown=True)
    assert 'SPY' not in target_intents('H3',f,{'SPY':HoldingState(9,.05)})
    assert target_intents('H3',f,{'SPY':HoldingState(10,.05)})['SPY'].target_weight==0
    assert 'SPY' not in target_intents('H3',f,{},blocked_symbols={'SPY'})
    f['SPY']=DailyFeature(False,'missing')
    assert target_intents('H3',f,{'SPY':HoldingState(63,.05)})['SPY'].reason=='maximum_hold_exit'


def test_inverse_vol_band_and_caps():
    f=features(volatility=.1)
    out=target_intents('H4',f,{})
    assert all(x.target_weight==pytest.approx(.05) for x in out.values())
    assert 'SPY' not in target_intents('H4',f,{'SPY':HoldingState(30,.04)})
    assert target_intents('H4',f,{'SPY':HoldingState(30,.03)})['SPY'].target_weight==pytest.approx(.05)


def test_h1_retains_quantity_without_monthly_rebalancing():
    f=features(trend=True)
    assert 'SPY' not in target_intents('H1',f,{'SPY':HoldingState(30,.01)})
