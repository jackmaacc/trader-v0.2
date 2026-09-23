import numpy as np
import pandas as pd
import pytest
from trader_engine.backtest.engine import BacktestEngine
from trader_engine.core.config import BacktestConfig,FeatureConfig,RiskConfig,load_config
from trader_engine.core.models import AssetClass,UniverseMember
from trader_engine.risk.engine import RiskManager


def frame(direction='long',periods=5):
 idx=pd.bdate_range('2024-01-01',periods=periods)
 return pd.DataFrame(dict(open=100.,high=101.,low=99.,close=100.,volume=1000000.,atr_14=5.,signal=[direction]+['flat']*(periods-1),signal_score=1.),index=idx)


def run(frames,**overrides):
 b=BacktestConfig(hold_bars=50,exit_on_state_change=False,exit_on_signal_flip=False,commission_bps=0,slippage_bps_equity=0)
 if 'backtest' in overrides:b=overrides.pop('backtest')
 risk=RiskConfig(max_position_pct=.5,max_trade_risk_pct=.0025,max_portfolio_stop_risk_pct=.01,**overrides)
 return BacktestEngine(b,RiskManager(risk),FeatureConfig()).run(frames,{s:UniverseMember(s,AssetClass.EQUITY) for s in frames},100000)


def test_same_batch_entries_share_one_budget():
 result=run({f'S{i}':frame() for i in range(6)})
 assert len(result.trades)==4
 assert result.trades.initial_stop_risk_dollars.sum()==pytest.approx(1000)
 assert result.orders.notes.fillna('').str.contains('budget exhausted').sum()==2
 assert result.equity_curve.modeled_stop_risk_dollars.max()==pytest.approx(1000)


def test_partial_last_position_is_reduced_and_explained():
 frames={f'S{i}':frame() for i in range(4)}
 risk=RiskConfig(max_position_pct=.5,max_trade_risk_pct=.004,max_portfolio_stop_risk_pct=.01)
 b=BacktestConfig(hold_bars=50,exit_on_state_change=False,exit_on_signal_flip=False,commission_bps=0,slippage_bps_equity=0)
 result=BacktestEngine(b,RiskManager(risk),FeatureConfig()).run(frames,{s:UniverseMember(s,AssetClass.EQUITY) for s in frames},100000)
 assert sorted(result.trades.initial_stop_risk_dollars)==pytest.approx([200,400,400])
 assert result.orders.notes.fillna('').str.contains('Position reduced').any()


def test_short_fill_uses_actual_slipped_price_for_budget():
 b=BacktestConfig(hold_bars=50,exit_on_state_change=False,exit_on_signal_flip=False,slippage_bps_equity=100,commission_bps=0)
 result=run({'S':frame('short')},backtest=b)
 assert result.trades.initial_stop_risk_dollars.iloc[0]==pytest.approx(250)


def test_budget_rejects_disabled_stops():
 with pytest.raises(ValueError,match='require enabled ATR stops'):
  run({'S':frame()},backtest=BacktestConfig(use_atr_stop=False))


def test_missing_held_current_quote_blocks_additional_entry():
 a=frame();b=frame();b['signal']='flat';b.loc[b.index[1],'signal']='long';a=a.drop(a.index[2])
 result=run({'A':a,'B':b})
 assert set(result.trades.symbol)=={'A'}
 assert result.orders.notes.fillna('').str.contains('missing current price').any()


def test_exit_can_free_risk_budget_before_new_entry():
 a=frame();b=frame();b['signal']='flat';b.loc[b.index[1],'signal']='long'
 cfg=BacktestConfig(hold_bars=50,exit_on_state_change=False,exit_on_signal_flip=True,commission_bps=0,slippage_bps_equity=0)
 result=run({'A':a,'B':b},backtest=cfg)
 assert set(result.trades.symbol)=={'A','B'}
 assert (result.trades.exit_reason=='signal_flip').all()


def test_opposing_positions_do_not_cancel_stop_risk():
 p={'A':dict(direction='long',quantity=10,stop_price=90),'B':dict(direction='short',quantity=10,stop_price=110)}
 assert RiskManager.open_stop_risk(p,{'A':100,'B':100})==200
 assert np.isinf(RiskManager.open_stop_risk(p,{'A':100}))


def test_conservative_profile_is_research_only():
 c=load_config('configs/risk_controlled.yaml')
 assert c.risk.max_trade_risk_pct==.0025 and c.risk.max_portfolio_stop_risk_pct==.01
 assert not c.execution.paper_enabled
 assert len(c.universe.equities)==95
 assert not set(c.universe.equities)&{'COSM','METC','DVLT','HSCS','SPRC'}
 assert c.resolved_for_asset_class(AssetClass.EQUITY).backtest.use_atr_stop


def test_portfolio_limit_accounts_for_entry_costs():
 b=BacktestConfig(hold_bars=50,exit_on_state_change=False,exit_on_signal_flip=False,commission_bps=100,slippage_bps_equity=0)
 result=run({f'S{i}':frame() for i in range(6)},backtest=b)
 eq=result.equity_curve.iloc[1]
 assert eq.modeled_stop_risk_dollars <= eq.equity*.01+1e-8


def test_walk_forward_supplies_pre_window_prices(monkeypatch):
 from test_robustness import _test_config,_synthetic_raw_frame
 from trader_engine.research.context import ResearchContext
 from trader_engine.research.walk_forward import WalkForwardRunner
 context=ResearchContext(_test_config())
 dataset=context.prepare_dataset({'X':UniverseMember('X',AssetClass.EQUITY)},{'X':_synthetic_raw_frame()})
 runner=WalkForwardRunner(context);prepared=runner.prepare(dataset)
 engine=context.build_backtest_engine();original=engine.run;seen=[]
 def capture(*args,**kwargs):
  history=kwargs['risk_history_by_symbol']['X'];window=kwargs['frames_by_symbol']['X']
  assert history.index[0]<window.index[0]
  seen.append(True)
  return original(*args,**kwargs)
 monkeypatch.setattr(engine,'run',capture)
 runner.run_prepared(prepared,backtest_engine=engine)
 assert len(seen)>=2
