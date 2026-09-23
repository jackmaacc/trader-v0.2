from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import numpy as np
import pandas as pd
import pytest
from trader_engine.research.strategy_spec import StrategySpec,ETF_UNIVERSE
from trader_engine.research.etf_sensitivity import prepare_ytd,run_scenario,compiled_library
from trader_engine.backtest.engine import BacktestEngine


def fixture_data(family='MOM',days=5):
    dates=pd.bdate_range('2026-06-01',periods=days,tz='UTC')
    schedule=pd.DataFrame({'market_open':dates+pd.Timedelta(hours=13,minutes=30),
        'market_close':dates+pd.Timedelta(hours=17)})
    # Explicit prior exchange dates avoid assuming weekday calendar in preparation.
    schedule['previous_session']=[pd.Timestamp('2026-05-29').date()]+[d.date() for d in dates[:-1]]
    frames={}
    for s in ETF_UNIVERSE:
        pieces=[]
        for d,row in enumerate(schedule.itertuples()):
            idx=pd.date_range(row.market_open,row.market_close-pd.Timedelta(minutes=1),freq='min')
            c=100+np.cumsum(np.random.default_rng(19+d).normal(0,.1,len(idx))) if family=='MR' else np.full(len(idx),100.)
            pieces.append(pd.DataFrame(dict(open=c,high=c+.1,low=c-.1,close=c,volume=10000.),index=idx))
        frames[s]=pd.concat(pieces)
    idx=pd.bdate_range(end=dates[-1],periods=100+days)
    c=100+np.arange(len(idx))*.1+np.sin(np.arange(len(idx)))*.03
    daily={s:pd.DataFrame(dict(high=c+1,low=c-1,close=c,total_return_close=c,signal_scale=1.),index=idx) for s in ETF_UNIVERSE}
    return frames,daily,schedule,pd.DataFrame()


@pytest.fixture(scope='module')
def library(tmp_path_factory):return compiled_library(tmp_path_factory.mktemp('cpp'))


def compare(frames,daily,schedule,actions,spec,delay,library):
    prepared=prepare_ytd(frames,daily,schedule,actions,spec.candidate_id)
    fast=run_scenario(prepared,spec,delay,library=library)
    original=BacktestEngine.run_etf_replay(frames,spec,schedule=schedule,daily_signal_frames=daily,corporate_actions=actions,execution_delay_minutes=delay)
    for field in ['final_equity','gross_pnl','net_pnl','fees','modeled_impact','dividend_receivable','dividends_paid']:
        assert fast.metrics[field]==pytest.approx(original.metrics[field],abs=1e-7,rel=1e-12),field
    for field in ['trade_count','open_positions','data_complete','valuation_valid','drawdown_halt']:
        assert fast.metrics[field]==original.metrics[field],field
    old=original.equity_curve.copy();old['session']=pd.to_datetime(old.timestamp,utc=True).dt.tz_convert('America/New_York').dt.date
    closes=old.groupby('session',sort=True).tail(1)
    for field in ['equity','cash','cumulative_gross_pnl','cumulative_net_pnl','dividend_receivable','dividends_paid']:
        np.testing.assert_allclose(fast.daily[field],closes[field],atol=1e-7,rtol=1e-12)
    entries=original.decisions.loc[original.decisions.action.isin(['entry','exit'])].reset_index(drop=True)
    events=fast.events
    assert len(entries)==len(events)
    for field in ['timestamp','symbol','action','reason']:
        assert entries[field].tolist()==events[field].tolist(),field
    for field in ['quantity','price']:np.testing.assert_allclose(entries[field],events[field],atol=1e-9,rtol=1e-12)
    exits=events.loc[events.action=='exit']
    np.testing.assert_allclose(exits.episode_net,original.trades.get('pnl',pd.Series(dtype=float)),atol=1e-7,rtol=1e-12)
    return prepared,fast,original


@pytest.mark.parametrize('family,delay,cost',[('MR30',0,0),('MR60',1,7),('MOM20',0,3),('MOM60',2,14)])
def test_shared_signals_fill_and_daily_ledger_parity(family,delay,cost,library):
    data=fixture_data('MR' if family.startswith('MR') else 'MOM')
    p,r,old=compare(*data,StrategySpec(family,slippage_bps=cost,commission_bps=2),delay,library)
    assert len(r.events)>0,'Fixture must exercise actual fills'
    with pytest.raises(ValueError):p.prices.flags.writeable=True


@pytest.mark.parametrize('payment',['paid','unpaid','after_sale'])
def test_split_dividend_and_settlement_parity(payment,library):
    frames,daily,schedule,_=fixture_data(days=6);ex=schedule.market_open.iloc[1]
    for f in frames.values():f.loc[f.index>=ex,['open','high','low','close']]/=2
    pay=ex if payment=='paid' else pd.NaT if payment=='unpaid' else schedule.market_open.iloc[5]
    actions=pd.DataFrame([dict(symbol=s,split_ratio=2.,cash_dividend=1.,payment_timestamp=pay) for s in ETF_UNIVERSE],index=pd.DatetimeIndex([ex]*5))
    _,r,_=compare(frames,daily,schedule,actions,StrategySpec('MOM20',commission_bps=2),1,library)
    assert r.metrics['dividends_paid']>0 if payment!='unpaid' else r.metrics['dividend_receivable']>0


def test_missing_marks_gap_halt_and_early_close(library):
    frames,daily,schedule,actions=fixture_data(days=3)
    frames['SPY']=frames['SPY'].drop(schedule.market_open.iloc[0]+pd.Timedelta(minutes=6))
    for f in frames.values():f.loc[f.index>=schedule.market_open.iloc[1],['open','high','low','close']]*=.3
    schedule.loc[2,'market_close']=schedule.market_open.iloc[2]+pd.Timedelta(minutes=75)
    _,r,_=compare(frames,daily,schedule,actions,StrategySpec('MOM20'),0,library)
    assert not r.metrics['data_complete'] and not r.metrics['valuation_valid']
    assert r.metrics['drawdown_halt'] and 'gap_stop' in set(r.events.reason)


def test_stale_daily_blocks_without_future_filter(library):
    frames,daily,schedule,actions=fixture_data(days=2)
    for s in daily:daily[s]=daily[s].drop(pd.Timestamp('2026-05-29',tz='UTC'))
    _,r,_=compare(frames,daily,schedule,actions,StrategySpec('MOM20'),0,library)
    assert not r.metrics['data_complete']


def test_invalid_inputs_rejected(library):
    frames,daily,schedule,actions=fixture_data(days=1)
    frames['SPY'].iloc[0,0]=-1
    with pytest.raises(ValueError,match='OHLCV'):prepare_ytd(frames,daily,schedule,actions,'MR30')
    frames,daily,schedule,actions=fixture_data(days=1)
    frames['SPY'].index=frames['SPY'].index.tz_localize(None)
    with pytest.raises(ValueError,match='timezone'):prepare_ytd(frames,daily,schedule,actions,'MR30')
    frames,daily,schedule,actions=fixture_data(days=1)
    p=prepare_ytd(frames,daily,schedule,actions,'MOM20')
    with pytest.raises(ValueError,match='overhead'):run_scenario(p,StrategySpec('MOM20',daily_overhead=3),0,library)


def test_shared_library_parallel_independent_scenarios(library):
    p=prepare_ytd(*fixture_data(days=2),'MOM20')
    specs=[StrategySpec('MOM20',slippage_bps=i) for i in [1,3,7,14]]
    expected=[run_scenario(p,s,1,library).metrics for s in specs]
    with ThreadPoolExecutor(max_workers=4) as pool:actual=list(pool.map(lambda s:run_scenario(p,s,1,library).metrics,specs))
    assert actual==expected
