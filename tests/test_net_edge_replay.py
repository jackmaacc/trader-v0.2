from dataclasses import FrozenInstanceError, replace
import numpy as np
import pandas as pd
import pytest
from trader_engine.research.strategy_spec import StrategySpec, ETF_UNIVERSE
from trader_engine.research.etf_candidates import mean_reversion_decision, momentum_decision
from trader_engine.backtest.engine import BacktestEngine
from trader_engine.research.walk_forward import build_session_folds, purge_overlapping_labels, run_etf_continuous, SessionFold


def fixture_data(n=5):
    days=pd.bdate_range('2026-06-01',periods=n)
    schedule=pd.DataFrame({'market_open':[d.tz_localize('UTC')+pd.Timedelta(hours=13,minutes=30) for d in days],
                           'market_close':[d.tz_localize('UTC')+pd.Timedelta(hours=20) for d in days]})
    indices=[]
    for _,r in schedule.iterrows():
        indices.extend([r.market_open,r.market_open+pd.Timedelta(minutes=5),r.market_close-pd.Timedelta(minutes=5),r.market_close-pd.Timedelta(minutes=1)])
    frames={s:pd.DataFrame(dict(open=100.,high=100.1,low=99.9,close=100.,volume=10000.),index=pd.DatetimeIndex(indices)) for s in ETF_UNIVERSE}
    dates=pd.bdate_range(end=days[-1],periods=100+n)
    close=100+np.arange(len(dates))*.1 + np.sin(np.arange(len(dates)))*.03
    daily={s:pd.DataFrame(dict(high=close+1,low=close-1,close=close,total_return_close=close),index=dates) for s in ETF_UNIVERSE}
    return frames,schedule,daily


def replay(n=5,**kwargs):
    frames,schedule,daily=fixture_data(n)
    return BacktestEngine.run_etf_replay(frames,StrategySpec('MOM20',slippage_bps=0,daily_overhead=0),schedule=schedule,daily_signal_frames=daily,**kwargs)


def test_spec_frozen_hash_and_defaults():
    spec=StrategySpec('MR30')
    with pytest.raises(FrozenInstanceError):spec.candidate_id='MR60'
    assert spec.spec_hash==StrategySpec('MR30').spec_hash
    assert spec.spec_hash!=replace(spec,slippage_bps=14).spec_hash
    assert spec.max_name==.1 and spec.max_overnight==.25
    assert spec.daily_overhead is None


def test_zero_denominators_no_signal():
    idx=pd.date_range('2026-06-01 13:30',periods=90,freq='min',tz='UTC')
    f=pd.DataFrame(dict(high=100.,low=100.,close=100.,volume=100.),index=idx)
    assert mean_reversion_decision(f,StrategySpec('MR30')).reason=='zero_denominator'
    f.index=pd.bdate_range('2026-01-01',periods=90)
    f['total_return_close']=100.
    assert not momentum_decision(f,StrategySpec('MOM20')).eligible


def test_five_session_exit_and_overnight_carry():
    result=replay(5)
    assert len(result.trades)==5
    assert set(result.trades.reason)=={'five_session_exit'}
    assert all(pd.to_datetime(result.trades.exit_time).dt.date==pd.Timestamp('2026-06-05').date())
    assert result.metrics['final_equity']==100000
    assert replay(2).metrics['open_positions']==5
    assert replay(2).trades.empty


def test_actions_split_dividend_cash_and_no_double_fee():
    frames,schedule,daily=fixture_data(5)
    action_time=schedule.market_open.iloc[1]
    for f in frames.values():f.loc[f.index>=action_time,['open','high','low','close']]/=2
    actions=pd.DataFrame([dict(symbol=s,split_ratio=2.,cash_dividend=1.) for s in ETF_UNIVERSE],index=pd.DatetimeIndex([action_time]*5))
    spec=StrategySpec('MOM20',slippage_bps=0,commission_bps=2,daily_overhead=3)
    r=BacktestEngine.run_etf_replay(frames,spec,schedule=schedule,daily_signal_frames=daily,corporate_actions=actions)
    assert len(r.trades)==5 and set(r.trades.reason)=={'five_session_exit'}
    assert r.metrics['fees']==pytest.approx(r.trades.fees.sum())
    assert r.metrics['gross_pnl']==pytest.approx(r.trades.quantity.sum())
    assert r.metrics['net_pnl']==pytest.approx(r.metrics['gross_pnl']-r.metrics['fees'])
    assert r.metrics['business_pnl']==pytest.approx(r.metrics['net_pnl']-15)
    assert r.metrics['final_equity']==pytest.approx(100000+r.metrics['business_pnl'])


def test_future_perturbation_does_not_change_prior_decisions():
    frames,schedule,daily=fixture_data(5)
    spec=StrategySpec('MOM20',slippage_bps=0)
    a=BacktestEngine.run_etf_replay(frames,spec,schedule=schedule,daily_signal_frames=daily)
    cutoff=schedule.market_open.iloc[3]
    for f in frames.values():f.loc[f.index>=cutoff,['open','high','low','close']]*=.95
    for f in daily.values():f.loc[f.index>=cutoff.tz_localize(None).normalize(),'total_return_close']*=.7
    b=BacktestEngine.run_etf_replay(frames,spec,schedule=schedule,daily_signal_frames=daily)
    pd.testing.assert_frame_equal(a.decisions.loc[a.decisions.timestamp<cutoff].reset_index(drop=True), b.decisions.loc[b.decisions.timestamp<cutoff].reset_index(drop=True))
    pd.testing.assert_frame_equal(a.equity_curve.loc[a.equity_curve.timestamp<cutoff].reset_index(drop=True),b.equity_curve.loc[b.equity_curve.timestamp<cutoff].reset_index(drop=True))


def test_gap_stop_uses_gap_price_and_daily_halt_carries_previous_close():
    frames,schedule,daily=fixture_data(2)
    for f in frames.values():f.loc[f.index>=schedule.market_open.iloc[1],['open','high','low','close']]*=.9
    r=BacktestEngine.run_etf_replay(frames,StrategySpec('MOM20',slippage_bps=0),schedule=schedule,daily_signal_frames=daily)
    assert set(r.trades.reason)=={'gap_stop'}
    assert r.metrics['net_pnl']<-500
    assert r.equity_curve.daily_halt.any()
    assert r.metrics['open_positions']==0


def test_folds_embargo_purge_and_continuous_account():
    folds=build_session_folds(pd.bdate_range('2020-01-01',periods=514))
    assert len(folds)==3
    assert len(folds[0].train_sessions)==252 and len(folds[0].test_sessions)==63
    tests=[d for f in folds for d in f.test_sessions]
    assert len(tests)==len(set(tests))
    labels=pd.DataFrame(dict(signal_session=['2020-01-01']*2,label_end_session=['2020-01-03','2020-01-10']))
    assert len(purge_overlapping_labels(labels,['2020-01-01'],'2020-01-10'))==1
    frames,schedule,daily=fixture_data(5)
    ds=tuple(schedule.market_open.dt.date)
    small=[SessionFold(1,(),(),ds[:2]),SessionFold(2,(),(),ds[2:])]
    r=run_etf_continuous(frames,StrategySpec('MOM20',slippage_bps=0,daily_overhead=10),schedule=schedule,folds=small,daily_signal_frames=daily)
    assert r.metrics['capital_initializations']==1
    assert r.metrics['final_equity']==99950
    assert len(r.trades)==5
    assert all(pd.to_datetime(r.trades.entry_time).dt.date==ds[0])


def test_unknown_overhead_not_claimed_as_business_profit():
    frames,schedule,daily=fixture_data(1)
    r=BacktestEngine.run_etf_replay(frames,StrategySpec('MOM20'),schedule=schedule,daily_signal_frames=daily)
    assert r.metrics['business_pnl'] is None and not r.metrics['overhead_known']


def test_vectorized_mr_matches_shared_decisions_and_is_causal():
    from trader_engine.research.etf_candidates import mean_reversion_session_decisions
    rng=np.random.default_rng(19)
    idx=pd.date_range('2026-06-01 13:30',periods=150,freq='min',tz='UTC')
    c=100+np.cumsum(rng.normal(0,.1,len(idx)))
    f=pd.DataFrame(dict(high=c+.1,low=c-.1,close=c,volume=100.),index=idx)
    spec=StrategySpec('MR30');all_decisions=mean_reversion_session_decisions(f,spec)
    for i in range(59,len(f)):
        direct=mean_reversion_decision(f.iloc[:i+1],spec)
        batch=all_decisions[f.index[i]]
        assert direct.eligible==batch.eligible and direct.reason==batch.reason
        assert direct.score==pytest.approx(batch.score)
        assert direct.atr==pytest.approx(batch.atr)
    altered=f.copy();altered.iloc[100:,:3]*=.5
    other=mean_reversion_session_decisions(altered,spec)
    assert all(all_decisions[t]==other[t] for t in f.index[:100])


def test_mr_missed_past_bar_blocks_only_when_it_becomes_known():
    from trader_engine.research.etf_candidates import mean_reversion_session_decisions
    idx=pd.date_range('2026-06-01 13:30',periods=90,freq='min',tz='UTC')
    c=100+np.sin(np.arange(len(idx)))
    f=pd.DataFrame(dict(high=c+.1,low=c-.1,close=c,volume=100.),index=idx)
    spec=StrategySpec('MR30')
    complete=mean_reversion_session_decisions(f,spec)
    missing=mean_reversion_session_decisions(f.drop(idx[70]),spec)
    assert all(complete[t]==missing[t] for t in idx[:70])
    assert missing[idx[71]].reason=='missing_minute'


def test_exclusion_delay_and_calendar_idle_overhead():
    frames,schedule,daily=fixture_data(6)
    spec=StrategySpec('MOM20',slippage_bps=0)
    # Add the explicitly delayed next-minute executable observations.
    for s,f in frames.items():
        extra=f.loc[f.index.minute==35].copy();extra.index+=pd.Timedelta(minutes=1)
        frames[s]=pd.concat([f,extra]).sort_index()
    costs={d.date():10. for d in pd.date_range('2026-06-01','2026-06-08')}
    r=BacktestEngine.run_etf_replay(frames,spec,schedule=schedule,daily_signal_frames=daily,
                                  excluded_symbols=('SPY',),execution_delay_minutes=1,calendar_overhead=costs)
    entries=r.decisions.loc[r.decisions.action=='entry']
    assert 'SPY' not in set(entries.symbol)
    assert all(entries.timestamp.dt.minute==36)
    assert r.metrics['overhead']==80 # Includes the weekend.
    assert r.metrics['business_pnl']==pytest.approx(-80)
    last=r.equity_curve.iloc[-1]
    assert last.broker_equity==pytest.approx(100000)
    assert last.equity==pytest.approx(99920)
    assert last.cumulative_net_pnl==pytest.approx(0)
    assert last.day_start_broker_equity==pytest.approx(100000)


def test_mr_pending_expires_when_due_observation_missing(monkeypatch):
    from trader_engine.research.etf_candidates import ETFDecision
    import trader_engine.research.etf_candidates as candidates
    frames,schedule,_=fixture_data(1)
    signal=schedule.market_open.iloc[0]+pd.Timedelta(minutes=5)
    def prepared(f,spec):
        return {t:ETFDecision(t==signal,'qualified' if t==signal else 'signal_filter',3.,1.,110.,t+pd.Timedelta(minutes=1)) for t in f.index}
    monkeypatch.setattr(candidates,'mean_reversion_session_decisions',prepared)
    r=BacktestEngine.run_etf_replay(frames,StrategySpec('MR30',slippage_bps=0),schedule=schedule)
    assert r.trades.empty and not r.positions
    assert 'pending_expired_missing_observation' in set(r.decisions.reason)
    assert 'entry' not in set(r.decisions.action)


def test_mr_delay_revalidates_only_prior_observation(monkeypatch):
    from trader_engine.research.etf_candidates import ETFDecision
    import trader_engine.research.etf_candidates as candidates
    frames,schedule,_=fixture_data(1)
    signal=schedule.market_open.iloc[0]+pd.Timedelta(minutes=5)
    for s,f in frames.items():
        extra=f.loc[[signal]].copy();extra.index+=pd.Timedelta(minutes=2)
        frames[s]=pd.concat([f,extra]).sort_index()
    monkeypatch.setattr(candidates,'mean_reversion_session_decisions',lambda f,spec:{t:ETFDecision(t==signal,'qualified',3.,1.,110.) for t in f.index})
    r=BacktestEngine.run_etf_replay(frames,StrategySpec('MR30'),schedule=schedule,execution_delay_minutes=1)
    assert 'delay_revalidation_failed' in set(r.decisions.reason)
    assert 'entry' not in set(r.decisions.action)


def test_stale_daily_signal_does_not_enter_and_terminal_marks_invalid():
    frames,schedule,daily=fixture_data(2)
    for s,f in daily.items():daily[s]=f.drop(pd.Timestamp('2026-06-01'))
    r=BacktestEngine.run_etf_replay(frames,StrategySpec('MOM20'),schedule=schedule,daily_signal_frames=daily,entry_sessions=['2026-06-02'])
    assert not r.positions and r.trades.empty
    r=replay(2)
    assert not r.metrics['data_complete'] and not r.metrics['valuation_valid']


def test_held_stop_risk_includes_all_modeled_exit_costs():
    from trader_engine.backtest.engine import _etf_mark_to_stop_risk
    expected=100*(100-99*(1-.001)*(1-.002))
    assert _etf_mark_to_stop_risk(100,100,99,.001,.002)==pytest.approx(expected)
    assert expected>100 # The naive price-to-stop risk omitted exit costs.


def test_invalid_stress_arguments_rejected():
    frames,schedule,daily=fixture_data(1)
    for kw in [dict(excluded_symbols=('BAD',)),dict(excluded_symbols=ETF_UNIVERSE),dict(execution_delay_minutes=-1),dict(calendar_overhead={})]:
        with pytest.raises(ValueError):
            BacktestEngine.run_etf_replay(frames,StrategySpec('MOM20'),schedule=schedule,daily_signal_frames=daily,**kw)


def test_strategy_cannot_weaken_mandated_limits():
    for kwargs in [dict(max_name=.11),dict(max_gross=.6),dict(max_trade_risk=.002),dict(drawdown_halt=.04)]:
        with pytest.raises(ValueError):StrategySpec('MOM20',**kwargs)


def test_unknown_dividend_payment_is_never_spendable():
    frames,schedule,daily=fixture_data(5)
    ex=schedule.market_open.iloc[1]
    actions=pd.DataFrame([dict(symbol=s,split_ratio=1.,cash_dividend=1.,payment_timestamp=pd.NaT) for s in ETF_UNIVERSE],index=pd.DatetimeIndex([ex]*5))
    r=BacktestEngine.run_etf_replay(frames,StrategySpec('MOM20',slippage_bps=0,daily_overhead=0),schedule=schedule,daily_signal_frames=daily,corporate_actions=actions)
    entitled=r.trades.quantity.sum()
    assert entitled>0 and not r.positions
    assert r.metrics['dividend_receivable']==pytest.approx(entitled)
    assert r.metrics['dividends_paid']==0
    assert r.equity_curve.iloc[-1].cash==pytest.approx(100000)
    assert r.metrics['final_equity']==pytest.approx(100000+entitled)
    assert r.metrics['net_pnl']==pytest.approx(entitled)
    before=r.equity_curve.loc[r.equity_curve.timestamp<=ex].iloc[-1]
    after=r.equity_curve.loc[r.equity_curve.timestamp>ex].iloc[0]
    assert after.cash==before.cash
    assert after.equity-before.equity==pytest.approx(entitled)


def test_dividend_payment_after_sale_settles_once():
    frames,schedule,daily=fixture_data(6)
    ex=schedule.market_open.iloc[1];pay=schedule.market_open.iloc[5]
    actions=pd.DataFrame([dict(symbol=s,split_ratio=1.,cash_dividend=1.,payment_timestamp=pay) for s in ETF_UNIVERSE],index=pd.DatetimeIndex([ex]*5))
    r=BacktestEngine.run_etf_replay(frames,StrategySpec('MOM20',slippage_bps=0,daily_overhead=0),schedule=schedule,daily_signal_frames=daily,corporate_actions=actions,entry_sessions=['2026-06-01'])
    entitled=r.trades.quantity.sum()
    assert entitled>0 and not r.positions
    before=r.equity_curve.loc[r.equity_curve.timestamp<=pay].iloc[-1]
    after=r.equity_curve.loc[r.equity_curve.timestamp>pay].iloc[0]
    assert before.open_positions==0 and before.dividend_receivable==pytest.approx(entitled)
    assert after.cash-before.cash==pytest.approx(entitled)
    assert after.equity==pytest.approx(before.equity)
    assert r.metrics['dividend_receivable']==0
    assert r.metrics['dividends_paid']==pytest.approx(entitled)
    assert r.equity_curve.iloc[-1].cash==pytest.approx(100000+entitled)
    assert r.metrics['net_pnl']==pytest.approx(entitled)


@pytest.mark.parametrize("portfolio_cap", [.005, .001])
def test_entry_stop_above_open_fills_at_open_and_sizes_full_loss(monkeypatch, portfolio_cap):
    from trader_engine.research.etf_candidates import ETFDecision
    import trader_engine.research.etf_candidates as candidates
    frames,schedule,_=fixture_data(1)
    signal=schedule.market_open.iloc[0]+pd.Timedelta(minutes=5)
    for symbol,f in frames.items():
        next_bar=f.loc[[signal]].copy();next_bar.index+=pd.Timedelta(minutes=1)
        frames[symbol]=pd.concat([f,next_bar]).sort_index()
    # $100.60 impacted entry, $0.015 stop distance => stop above $100 raw open.
    # Budget must cover the entire executable roundtrip, not the smaller ATR distance.
    monkeypatch.setattr(candidates,'mean_reversion_session_decisions',lambda f,spec:{
        t:ETFDecision(t==signal,'qualified',3.,.01,110.) for t in f.index})
    spec=StrategySpec('MR30',slippage_bps=60,commission_bps=2,max_portfolio_risk=portfolio_cap)
    r=BacktestEngine.run_etf_replay(frames,spec,schedule=schedule)
    assert len(r.trades)==5
    exits=r.decisions.loc[r.decisions.action=='exit']
    assert set(exits.reason)=={'stop'}
    assert np.allclose(exits.price,100*(1-spec.one_way_impact),rtol=0,atol=1e-12)
    eq=100000.
    entries=r.decisions.loc[r.decisions.action=='entry'].reset_index(drop=True)
    for i,entry in entries.iterrows():
        trade=r.trades.loc[r.trades.symbol==entry.symbol].iloc[0]
        risk_budget=eq*.001
        assert -trade.pnl<=risk_budget+1e-8
        # Entries are accepted before the simultaneous intrabar stop processing.
        eq-=entry.quantity*(entry.price*(1+spec.commission_rate)-100)

    # Remaining liquidation risk of simultaneous entries fits the marked-equity cap.
    quantity=entries.quantity.sum()
    equity_after_entries=100000-quantity*(100.6*(1+spec.commission_rate)-100)
    remaining_exit_risk=quantity*(100-100*(1-spec.one_way_impact)*(1-spec.commission_rate))
    assert remaining_exit_risk<=portfolio_cap*equity_after_entries+1e-8
