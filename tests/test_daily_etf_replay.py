import numpy as np
import pandas as pd
import pytest
from trader_engine.backtest.engine import BacktestEngine
from trader_engine.research.strategy_spec import ETF_UNIVERSE


def fixture(n=310):
    dates=pd.bdate_range('2020-01-01',periods=n)
    opens=pd.DatetimeIndex(dates).tz_localize('America/New_York')+pd.Timedelta(hours=9,minutes=30)
    schedule=pd.DataFrame(dict(market_open=opens.tz_convert('UTC'),market_close=(opens+pd.Timedelta(hours=6,minutes=30)).tz_convert('UTC')),index=dates)
    c=100+np.arange(n)*.05+np.sin(np.arange(n))*.001
    f=pd.DataFrame(dict(open=c,high=c+1,low=c-1,close=c,total_return_close=c,volume=1e6),index=dates)
    return {s:f.copy() for s in ETF_UNIVERSE},schedule


def run(frames,cal,candidate='H3',**kw):
    return BacktestEngine.run_daily_etf_replay(frames,candidate,schedule=cal,start=cal.index[220],end=cal.index[-2],**kw)


def test_next_open_fills_continuous_ledger_and_cost_identity():
    frames,cal=fixture();result=run(frames,cal)
    entries=result.decisions.query("action == 'entry'")
    assert not entries.empty
    assert entries.timestamp.min()==cal.market_open.iloc[221]
    plans=result.decisions.query("action == 'plan'")
    assert plans.timestamp.min()==cal.market_close.iloc[220]
    curve=result.equity_curve
    assert len(curve)==89
    assert np.allclose(curve.cumulative_gross_pnl-curve.cumulative_impact_cost,curve.cumulative_net_pnl)
    assert result.metrics['net_pnl']==pytest.approx(result.symbol_contributions.net_pnl.sum())
    assert (curve.cash>=0).all()
    assert (curve.gross_exposure/curve.equity<=.251).all()
    assert result.metrics['overhead_known'] is False


def test_gap_below_signal_stop_cancels_entry_without_phantom_stop_fill():
    frames,cal=fixture()
    for f in frames.values():f.loc[cal.index[221],['open','high','low','close']]=[90,91,89,90]
    result=run(frames,cal)
    first=result.decisions[result.decisions.timestamp==cal.market_open.iloc[221]]
    assert (first.reason=='entry_open_breaches_signal_stop').sum()==5
    assert not (first.action=='entry').any()


def test_intraday_low_does_not_trigger_completed_close_stop():
    frames,cal=fixture()
    for f in frames.values():f.loc[cal.index[222],'low']=1
    result=run(frames,cal)
    assert not (result.decisions.reason=='protective_exit').any()


def test_close_stop_exits_next_open_and_gap_loss_is_real():
    frames,cal=fixture()
    for f in frames.values():
        f.loc[cal.index[222],['open','high','low','close']]=[111,112,90,90]
        f.loc[cal.index[223],['open','high','low','close']]=[80,81,79,80]
    result=run(frames,cal)
    exits=result.decisions.query("action == 'exit'")
    assert len(exits)>=5
    assert (exits.iloc[:5].timestamp==cal.market_open.iloc[223]).all()
    assert np.allclose(exits.iloc[:5].raw_price,80)
    assert result.metrics['net_pnl']<0


def test_delay_adds_session_without_resizing_up():
    frames,cal=fixture()
    a=run(frames,cal);b=run(frames,cal,delay_sessions=1)
    ea=a.decisions.query("action == 'entry'").iloc[:5]
    eb=b.decisions.query("action == 'entry'").iloc[:5]
    assert ea.timestamp.min()==cal.market_open.iloc[221]
    assert eb.timestamp.min()==cal.market_open.iloc[222]
    assert (eb.quantity.to_numpy()<=ea.quantity.to_numpy()).all()


def test_passive_receivable_survives_and_unknown_payment_never_becomes_cash():
    frames,cal=fixture()
    actions=pd.DataFrame([dict(symbol='SPY',split_ratio=1.,cash_dividend=2.,payment_timestamp=pd.NaT)],index=pd.DatetimeIndex([cal.market_open.iloc[225]]))
    r=run(frames,cal,corporate_actions=actions,benchmark_weight=1.)
    qty=r.positions['SPY']['qty']
    assert r.metrics['dividend_receivable']==pytest.approx(qty*2)
    assert r.metrics['dividends_paid']==0
    assert r.trades.empty
    assert r.metrics['hypothetical_liquidation_equity']<r.metrics['final_equity']


def test_split_normalizes_atr_adjusts_units_and_preserves_economics():
    frames,cal=fixture();plain=run(frames,cal,benchmark_weight=.25)
    date=cal.index[230]
    for f in frames.values():f.loc[date:,['open','high','low','close']]/=2
    acts=pd.DataFrame([dict(symbol=s,split_ratio=2.,cash_dividend=0.) for s in ETF_UNIVERSE],index=pd.DatetimeIndex([cal.market_open.iloc[230]]*5))
    split=run(frames,cal,corporate_actions=acts,benchmark_weight=.25)
    assert split.metrics['net_pnl']==pytest.approx(plain.metrics['net_pnl'])
    for s in ETF_UNIVERSE:assert split.positions[s]['qty']==2*plain.positions[s]['qty']


def test_duplicate_action_and_missing_benchmark_open_fail_closed():
    frames,cal=fixture()
    acts=pd.DataFrame([dict(symbol='QQQ',cash_dividend=1.)]*2,index=pd.DatetimeIndex([cal.market_open.iloc[225]]*2))
    with pytest.raises(ValueError,match='duplicate'):run(frames,cal,corporate_actions=acts)
    frames['QQQ']=frames['QQQ'].drop(cal.index[220])
    with pytest.raises(ValueError,match='initial raw open missing'):run(frames,cal,benchmark_weight=1.)


def test_halt_liquidates_once_even_with_extra_execution_delay():
    frames,cal=fixture()
    for f in frames.values():f.loc[cal.index[223]:,['open','high','low','close']]=[20,21,19,20]
    r=run(frames,cal,delay_sessions=1)
    exits=r.decisions.query("action == 'exit'")
    assert len(exits)==5
    assert (exits.timestamp==cal.market_open.iloc[225]).all()
    assert r.metrics['drawdown_halt']
    assert r.metrics['open_positions']==0
    assert len(r.equity_curve)==89


def test_missing_entry_open_expires_but_missing_exit_open_remains_pending():
    frames,cal=fixture()
    frames['SPY']=frames['SPY'].drop(cal.index[221])
    r=run(frames,cal)
    assert (r.decisions.reason=='missing_entry_open_expired').any()
    entries=r.decisions.query("symbol == 'SPY' and action == 'entry'")
    assert entries.timestamp.min()>=cal.market_open.iloc[286]
    frames,cal=fixture()
    for f in frames.values():f.loc[cal.index[222]:,['open','high','low','close']]=[20,21,19,20]
    frames['SPY']=frames['SPY'].drop(cal.index[223])
    r=run(frames,cal)
    spy=r.decisions.query("symbol == 'SPY' and action == 'exit'")
    assert spy.timestamp.iloc[0]==cal.market_open.iloc[224]
    assert not r.metrics['valuation_valid']


def test_partial_reduction_retains_episode_until_actual_full_exit():
    frames,cal=fixture()
    # One winner breaches the name cap; reduce at an actual later open.
    frames['SPY'].loc[cal.index[223]:,['open','high','low','close']]=[2000,2001,1999,2000]
    r=run(frames,cal,delay_sessions=1)
    reductions=r.decisions.query("symbol == 'SPY' and action == 'reduce'")
    assert not reductions.empty
    assert reductions.timestamp.iloc[0]==cal.market_open.iloc[225]
    assert r.metrics['net_pnl']==pytest.approx(r.symbol_contributions.net_pnl.sum())

@pytest.mark.parametrize('candidate',['H1','H2','H4'])
def test_other_frozen_families_share_accounting_and_month_week_cadence(candidate):
    frames,cal=fixture()
    r=run(frames,cal,candidate=candidate)
    assert r.metrics['net_pnl']==pytest.approx(r.symbol_contributions.net_pnl.sum())
    assert (r.equity_curve.cash>=0).all()
    assert not r.decisions.query("action == 'entry'").empty


def test_dividend_payment_after_sale_remains_owned_and_settles_once():
    frames,cal=fixture()
    for f in frames.values():f.loc[cal.index[226]:,['open','high','low','close']]=[20,21,19,20]
    acts=pd.DataFrame([dict(symbol='SPY',split_ratio=1.,cash_dividend=2.,payment_timestamp=cal.market_open.iloc[240])],index=pd.DatetimeIndex([cal.market_open.iloc[225]]))
    r=run(frames,cal,corporate_actions=acts)
    qty=r.decisions.query("symbol == 'SPY' and action == 'entry'").quantity.iloc[0]
    assert r.metrics['dividends_paid']==pytest.approx(qty*2)
    assert r.metrics['dividend_receivable']==0
    curve=r.equity_curve.set_index('session')
    assert curve.loc[cal.index[228].date(),'dividend_receivable']==pytest.approx(qty*2)
    assert curve.loc[cal.index[240].date(),'dividend_receivable']==0
    assert r.metrics['net_pnl']==pytest.approx(r.symbol_contributions.net_pnl.sum())


def test_split_keeps_protective_stop_and_signal_atr_on_same_scale():
    frames,cal=fixture();plain=run(frames,cal)
    date=cal.index[230]
    for f in frames.values():f.loc[date:,['open','high','low','close']]/=2
    acts=pd.DataFrame([dict(symbol=s,split_ratio=2.,cash_dividend=0.) for s in ETF_UNIVERSE],index=pd.DatetimeIndex([cal.market_open.iloc[230]]*5))
    split=run(frames,cal,corporate_actions=acts)
    assert not (split.decisions.reason=='protective_exit').any()
    assert split.metrics['net_pnl']==pytest.approx(plain.metrics['net_pnl'],abs=1.)
