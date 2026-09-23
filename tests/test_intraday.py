import pandas as pd,numpy as np,pytest
from trader_engine.intraday.engine import IntradayConfig,signals,run_intraday,audit_coverage

def fixture(n=60):
 start=pd.Timestamp('2026-09-01 13:30',tz='UTC');idx=pd.date_range(start,periods=n,freq='min')
 f=pd.DataFrame({'open':100.,'high':100.1,'low':99.9,'close':100.,'volume':100000.},index=idx)
 return f,pd.DataFrame({'open':[start],'close':[start+pd.Timedelta(minutes=n)]})

def forced(at=(20,),atr=1.):
 def build(f,c):
  r=pd.DataFrame({'entry':False,'atr':atr,'score':2.,'setup':'test'},index=f.index);r.iloc[list(at),r.columns.get_loc('entry')]=True
  return r
 return build

def config(**kwargs):return IntradayConfig(**({'max_correlation':None,'min_reward_cost_multiple':1}|kwargs))

def test_delayed_execution_and_flatten_early_session():
 f,cal=fixture();r=run_intraday({'A':f},cal,config(max_hold_minutes=100),signal_builder=forced());t=r.trades.iloc[0]
 assert t.entry_at==f.index[22] and t.signal_at==f.index[21]
 assert t.exit_at==f.index[55] and t.exit_reason=='session_flatten'
 assert r.equity.open_positions.iloc[-1]==0
 assert r.metrics['net_pnl']==pytest.approx(-r.trades.costs.sum())
 assert r.metrics['daily_close_drawdown']<0

def test_stop_before_target_on_ambiguous_bar():
 f,cal=fixture();f.loc[f.index[23],['high','low']]=[105.,95.]
 r=run_intraday({'A':f},cal,config(),signal_builder=forced())
 assert r.trades.exit_reason.iloc[0]=='stop' and r.trades.pnl.iloc[0]<0

def test_gap_stop_uses_actual_open():
 f,cal=fixture();f.loc[f.index[23],['open','high','low','close']]=[95,96,94,95]
 r=run_intraday({'A':f},cal,config(),signal_builder=forced())
 assert r.trades.exit_reason.iloc[0]=='gap_stop' and r.trades.exit_price.iloc[0]==95

def test_daily_loss_latches_and_blocks_reentry():
 f,cal=fixture();f.loc[f.index[23],['open','high','low','close']]=[90,91,89,90]
 r=run_intraday({'A':f},cal,config(daily_loss_limit=.001),signal_builder=forced(at=(20,25,35)))
 assert len(r.trades)==1 and r.daily.halted.iloc[0]
 assert r.trades.exit_reason.iloc[0]=='daily_loss_halt'

def test_limits_and_cooldown():
 f,cal=fixture();r=run_intraday({'A':f},cal,config(max_hold_minutes=1,max_entries_per_day=2,cooldown_minutes=5),signal_builder=forced(at=tuple(range(20,45))))
 assert len(r.trades)==2
 assert (r.trades.entry_at.iloc[1]-r.trades.exit_at.iloc[0])>=pd.Timedelta(minutes=5)
 assert r.decisions.reason.isin(['cooldown','entry_limit']).any()

def test_portfolio_budget_and_integer_participation():
 f,cal=fixture();r=run_intraday({s:f for s in ['A','B','C','D']},cal,config(portfolio_stop_risk=.0015),signal_builder=forced())
 assert r.trades.initial_risk.sum()<=150
 assert ((r.trades.quantity%1)==0).all() and (r.trades.quantity<=1000).all()

def test_missing_bar_and_naive_index_rejected():
 f,cal=fixture()
 assert len(audit_coverage({'A':f.drop(f.index[3])},cal))==1
 with pytest.raises(ValueError,match='Missing minute'):run_intraday({'A':f.drop(f.index[3])},cal)
 f.index=f.index.tz_localize(None)
 with pytest.raises(ValueError,match='timezone-aware'):run_intraday({'A':f},cal)

def test_future_changes_do_not_change_prior_signals_or_positions():
 f,cal=fixture(100);f['close']=100+np.sin(np.arange(100)/3)*.2;f['high']=f[['open','close']].max(axis=1)+.1;f['low']=f[['open','close']].min(axis=1)-.1
 future=f.copy();future.iloc[70:,future.columns.get_indexer(['open','high','low','close'])]*=2
 pd.testing.assert_frame_equal(signals(f,config()).iloc[:70],signals(future,config()).iloc[:70])
 a=run_intraday({'A':f},cal,config(),signal_builder=forced());b=run_intraday({'A':future},cal,config(),signal_builder=forced())
 pd.testing.assert_frame_equal(a.equity.iloc[:70],b.equity.iloc[:70])

def test_no_overnight_and_daily_accounting():
 f,cal=fixture();nextf=f.copy();nextf.index+=pd.Timedelta(days=1);cal2=cal.copy();cal2['open']+=pd.Timedelta(days=1);cal2['close']+=pd.Timedelta(days=1)
 r=run_intraday({'A':pd.concat([f,nextf])},pd.concat([cal,cal2]),config(),signal_builder=forced())
 assert len(r.trades)==2 and r.daily.net_pnl.sum()==pytest.approx(r.trades.pnl.sum())
 assert r.trades.entry_at.dt.date.equals(r.trades.exit_at.dt.date)

def test_constant_correlation_fails_closed_for_second_position():
 f,cal=fixture();r=run_intraday({'A':f,'B':f},cal,IntradayConfig(),signal_builder=forced(at=(35,)))
 assert len(r.trades)==1 and 'correlation_or_insufficient_history' in r.decisions.reason.tolist()

def test_target_too_small_relative_to_costs_is_rejected():
 f,cal=fixture();r=run_intraday({'A':f},cal,IntradayConfig(),signal_builder=forced(atr=.01))
 assert r.trades.empty and 'insufficient_reward_after_costs' in r.decisions.reason.tolist()

def test_benchmark_costs_and_same_session_close():
 from trader_engine.intraday.engine import intraday_benchmark
 f,cal=fixture();c=config();b=intraday_benchmark({'A':f},cal,c)
 assert b.net_pnl.iloc[0]<0
 assert b.net_pnl.iloc[0]==pytest.approx(-int(100000*.5/(1+.0007)/100)*100*.0014)

def test_exchange_calendar_timezone_early_close():
 from trader_engine.intraday.engine import validate_schedule
 c=pd.DataFrame({'open':[pd.Timestamp('2026-11-27 09:30',tz='America/New_York')],'close':[pd.Timestamp('2026-11-27 13:00',tz='America/New_York')]})
 s=validate_schedule(c)
 assert s.close.iloc[0]-s.open.iloc[0]==pd.Timedelta(minutes=210)
 assert s.open.iloc[0].hour==14
