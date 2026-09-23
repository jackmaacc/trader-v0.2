import numpy as np
import pandas as pd
import pytest
from trader_engine.research.relative_value import RelativeValueConfig,make_forecasts,run_relative_value,validate_inputs
from trader_engine.data.relative_value_data import download_relative_data

def frames(n=320):
    dates=pd.bdate_range('2023-01-02',periods=n);rng=np.random.default_rng(12)
    base=100*np.exp(np.cumsum(rng.normal(0,.012,n)))
    result={}
    for name,prices in [('A',base*np.exp(rng.normal(0,.004,n))),('B',base)]:
        result[name]=pd.DataFrame(dict(open=prices,high=prices*1.01,low=prices*.99,close=prices,volume=100000),index=dates)
    return result,dates

def forced(d,indices=(0,)):
    return pd.DataFrame([dict(pair='A/B',a='A',b='B',signal_at=d[i],beta=1.,prediction=.03,mean_prediction_se=0.,last_training_label_at=d[i]) for i in indices])

def test_causal_forecasts_and_mature_labels():
    f,d=frames();c=RelativeValueConfig(feature_lookback=20,training_lookback=60,minimum_labels=20,hold_sessions=3)
    first=make_forecasts(f,[['A','B']],c);assert len(first)>100
    changed={s:v.copy() for s,v in f.items()}
    changed['A'].loc[d[200]:,['open','high','low','close']]*=1.3
    second=make_forecasts(changed,[['A','B']],c)
    cols=['signal_at','feature','prediction','mean_prediction_se','beta','last_training_label_at']
    pd.testing.assert_frame_equal(first.loc[first.signal_at<d[200],cols].reset_index(drop=True),second.loc[second.signal_at<d[200],cols].reset_index(drop=True))
    assert (first.last_training_label_at<=first.signal_at).all()
    assert first.training_labels.max()<=60

def test_next_open_horizon_and_accounting():
    f,d=frames(12);c=RelativeValueConfig(pair_loss_limit=1)
    r=run_relative_value(f,d,[['A','B']],forced(d),c,d[0],d[-1]);t=r.trades.iloc[0]
    assert t.entry_at==d[1] and t.exit_at==d[6]
    assert t.exit_reason=='horizon_exit'
    qa=t.entry_gross/2/f['A'].loc[d[1],'open'];qb=-t.entry_gross/2/f['B'].loc[d[1],'open']
    gross=qa*(f['A'].loc[d[6],'open']-f['A'].loc[d[1],'open'])+qb*(f['B'].loc[d[6],'open']-f['B'].loc[d[1],'open'])
    assert t.gross_pnl==pytest.approx(gross)
    borrow=sum(-qb*f['B'].loc[d[i-1],'close']*.03*(d[i]-d[i-1]).days/365 for i in range(2,7))
    assert t.borrow_cost==pytest.approx(borrow)
    assert r.equity.equity.iloc[-1]-100000==pytest.approx(t.gross_pnl-t.trading_costs-borrow)

def test_overlap_final_exit_and_costs():
    f,d=frames(5);c=RelativeValueConfig(pair_loss_limit=1)
    r=run_relative_value(f,d,[['A','B']],forced(d,(0,1,2)),c,d[0],d[-1])
    assert len(r.trades)==1 and r.trades.iloc[0].exit_reason=='evaluation_end'
    assert (r.decisions.reason=='portfolio_overlap_or_position_cap').sum()==2
    assert r.trades.trading_costs.sum()>0

def test_no_edge_is_cash():
    f,d=frames(8);fc=forced(d);fc.prediction=0
    r=run_relative_value(f,d,[['A','B']],fc,RelativeValueConfig(),d[0],d[-1])
    assert r.metrics['net_pnl']==0 and r.metrics['trades']==0

def test_reject_future_labels_and_missing_dates():
    f,d=frames(8);fc=forced(d);fc.last_training_label_at=d[1]
    with pytest.raises(ValueError,match='Future training'):run_relative_value(f,d,[['A','B']],fc,RelativeValueConfig(),d[0],d[-1])
    f['A']=f['A'].iloc[1:]
    with pytest.raises(ValueError,match='calendar'):validate_inputs(f,d,[['A','B']])

def test_data_contract(tmp_path):
    def fetch(endpoint,params):
        if endpoint=='calendar':return [{'date':'2024-03-11','close':'16:00'}]
        assert params['adjustment']=='all' and params['feed']=='sip'
        return {'bars':{'A':[dict(t='2024-03-11T04:00:00Z',o=100,h=101,l=99,c=100,v=1000)]}}
    m=download_relative_data(['A'],'2024-03-11','2024-03-11',tmp_path/'data',fetcher=fetch)
    assert 'A.parquet' in m['hashes']
    assert pd.read_parquet(tmp_path/'data/A.parquet').index[0]==pd.Timestamp('2024-03-11')

def test_pair_loss_exits_at_next_open():
    f,d=frames(9)
    for s in f:f[s].loc[:,['open','close']]=100;f[s]['high']=101;f[s]['low']=99
    f['A'].loc[d[2],'close']=90;f['A'].loc[d[2],'low']=89
    f['A'].loc[d[3]:,['open','close']]=90;f['A'].loc[d[3]:,'low']=89
    r=run_relative_value(f,d,[['A','B']],forced(d),RelativeValueConfig(),d[0],d[-1])
    assert r.trades.iloc[0].exit_at==d[3]
    assert r.trades.iloc[0].exit_reason=='pair_loss_limit'

def test_account_drawdown_latches():
    f,d=frames(10)
    for s in f:f[s].loc[:,['open','close']]=100;f[s]['high']=101;f[s]['low']=99
    f['A'].loc[d[2]:,['open','close']]=50;f['A'].loc[d[2]:,'low']=49
    c=RelativeValueConfig(account_drawdown_limit=.01)
    r=run_relative_value(f,d,[['A','B']],forced(d,(0,3,5)),c,d[0],d[-1])
    assert len(r.trades)==1 and r.trades.iloc[0].exit_reason=='account_halt'
    assert r.equity.loc[d[2]:,'drawdown_halted'].all()
