import numpy as np
import pandas as pd
import pytest
from test_relative_value import frames,forced
from trader_engine.research.relative_value import RelativeValueConfig,make_forecasts
from trader_engine.research.daily_relative_value import run_daily_relative_value

def test_daily_entries_exit_same_day_including_final_session():
    f,d=frames(9);fc=forced(d,tuple(range(8)));c=RelativeValueConfig()
    r=run_daily_relative_value(f,d,[['A','B']],fc,c,d[1],d[-1])
    assert len(r.trades)==8
    assert (r.trades.entry_at==r.trades.exit_at).all()
    assert (r.trades.signal_at<r.trades.entry_at).all()
    assert (r.equity.open_pairs_at_close==0).all()
    assert r.metrics['net_pnl']==pytest.approx(-r.trades.trading_costs.sum()-r.trades.borrow_cost.sum())
    assert r.trades.borrow_cost.sum()>0

def test_forced_daily_is_separate_from_cost_filter():
    f,d=frames(8);fc=forced(d,tuple(range(7)));fc.prediction=.000001;c=RelativeValueConfig()
    regular=run_daily_relative_value(f,d,[['A','B']],fc,c,d[1],d[-1])
    forced_run=run_daily_relative_value(f,d,[['A','B']],fc,c,d[1],d[-1],require_daily_trade=True)
    assert regular.metrics['trades']==0
    assert forced_run.metrics['trades']==7
    assert forced_run.metrics['net_pnl']<0

def test_same_day_label_matches_actual_hedged_return_and_is_causal():
    f,d=frames();c=RelativeValueConfig(feature_lookback=20,training_lookback=60,minimum_labels=20)
    f['A']['close']*=1+np.sin(np.arange(len(d)))*.001
    allf=make_forecasts(f,[['A','B']],c,same_day_exit=True)
    prefix=make_forecasts({s:v.loc[:d[200]] for s,v in f.items()},[['A','B']],c,same_day_exit=True)
    cols=['signal_at','prediction','mean_prediction_se','feature','beta','last_training_label_at']
    pd.testing.assert_frame_equal(allf.loc[allf.signal_at<=d[200],cols].reset_index(drop=True),prefix[cols].reset_index(drop=True))
    row=allf.iloc[0];exit_day=d[d.get_loc(row.signal_at)+1]
    expected=((f['A'].loc[exit_day,'close']/f['A'].loc[exit_day,'open']-1)-row.beta*(f['B'].loc[exit_day,'close']/f['B'].loc[exit_day,'open']-1))/(1+row.beta)
    assert row.label==pytest.approx(expected)
    assert row.label_available_at==exit_day
    assert (allf.last_training_label_at<=allf.signal_at).all()

def test_daily_drawdown_halt_blocks_later_forced_entries():
    f,d=frames(10)
    f['A'].loc[d[1],'close']=f['A'].loc[d[1],'open']*.5
    f['A'].loc[d[1],'low']=f['A'].loc[d[1],'close']*.99
    c=RelativeValueConfig(account_drawdown_limit=.01)
    r=run_daily_relative_value(f,d,[['A','B']],forced(d,tuple(range(9))),c,d[1],d[-1],require_daily_trade=True)
    assert len(r.trades)==1 and r.equity.drawdown_halted.all()

def test_daily_cash_reconciles_independent_two_leg_return():
    f,d=frames(3);f['A'].loc[d[1],'close']*=1.005
    r=run_daily_relative_value(f,d,[['A','B']],forced(d),RelativeValueConfig(),d[1],d[-1]);t=r.trades.iloc[0]
    gross=t.quantity_a*(f['A'].loc[d[1],'close']-f['A'].loc[d[1],'open'])+t.quantity_b*(f['B'].loc[d[1],'close']-f['B'].loc[d[1],'open'])
    assert r.metrics['net_pnl']==pytest.approx(gross-t.trading_costs-t.borrow_cost)
