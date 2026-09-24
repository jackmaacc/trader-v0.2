import numpy as np
import pandas as pd
import pytest
from trader_engine.research.daily_sensitivity import prepare,run_batch,assert_reference_parity,SYMBOLS


def fixture():
    d=pd.bdate_range('2024-01-01',periods=350);o=d.tz_localize('America/New_York')+pd.Timedelta(hours=9,minutes=30)
    cal=pd.DataFrame(dict(market_open=o.tz_convert('UTC'),market_close=(o+pd.Timedelta(hours=6,minutes=30)).tz_convert('UTC')))
    c=100+np.arange(len(d))*.07
    f=pd.DataFrame(dict(open=c,high=c+1,low=c-1,close=c,total_return_close=c),index=d)
    return {s:f.copy() for s in SYMBOLS},cal,pd.DataFrame(columns=['symbol'],index=pd.DatetimeIndex([],tz='UTC')),d


@pytest.mark.parametrize('cost,delay',[(0,0),(7,0),(14,1),(28,1)])
@pytest.mark.parametrize('case',['normal','crash','distribution_split'])
def test_reference_parity(cost,delay,case):
    fs,cal,acts,dates=fixture()
    if case=='crash':
        for f in fs.values():
            f.loc[dates[290]:,['open','high','low','close','total_return_close']]*=.2
    if case=='distribution_split':
        for f in fs.values():f.loc[dates[280]:,['open','high','low','close']]/=2
        acts=pd.DataFrame([dict(symbol=s,split_ratio=2.,cash_dividend=.5,payment_timestamp=cal.market_open.iloc[283] if j else pd.NaT) for j,s in enumerate(SYMBOLS)],index=pd.DatetimeIndex([cal.market_open.iloc[280]]*5))
    p=prepare(fs,cal,acts,dates[220],dates[-1])
    assert assert_reference_parity(p,fs,cal,acts,cost,delay)['passed']


@pytest.mark.parametrize('weight',[.25,1.])
def test_passive_parity(weight):
    fs,cal,acts,dates=fixture();p=prepare(fs,cal,acts,dates[220],dates[-1])
    assert assert_reference_parity(p,fs,cal,acts,7,1,weight)['passed']


def test_missing_day_fails_closed():
    fs,cal,acts,dates=fixture();fs['SPY']=fs['SPY'].drop(dates[230])
    with pytest.raises(ValueError,match='complete daily'):prepare(fs,cal,acts,dates[220],dates[-1])


def test_scenarios_are_independent_and_warmup_has_no_pending_order():
    fs,cal,acts,dates=fixture();p=prepare(fs,cal,acts,dates[220],dates[-1]);batch=run_batch(p,[1,14,28],[0,1,0])
    assert (batch['daily'][:,0,7]==0).all()
    for k,(c,d) in enumerate([(1,0),(14,1),(28,0)]):np.testing.assert_array_equal(batch['daily'][k],run_batch(p,[c],[d])['daily'][0])
