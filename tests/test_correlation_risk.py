import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError
from trader_engine.risk.engine import RiskManager
from trader_engine.core.config import RiskConfig,BacktestConfig,FeatureConfig
from trader_engine.backtest.engine import BacktestEngine
from trader_engine.core.models import AssetClass,UniverseMember


def histories():
    index=pd.bdate_range('2024-01-01',periods=12)
    a=pd.Series(100*np.cumprod(1+np.array([0,.01,-.02,.015,-.005,.03,-.01,.02,.005,-.015,.01,.02])),index=index)
    return {'A':a,'B':2*a},index


def test_guard_uses_signal_time_not_future_prices():
    h,index=histories();risk=RiskManager(RiskConfig(max_pairwise_correlation=.6,correlation_lookback_bars=5))
    positions={'A':{'direction':'long'}}
    reason=risk.correlation_rejection('B','long',positions,h,index[6]);assert reason
    h['B'].loc[index[7]:]=[400,20,500,10,1000]
    assert risk.correlation_rejection('B','long',positions,h,index[6])==reason


def test_guard_defaults_off_and_hedge_direction_is_respected():
    h,index=histories();positions={'A':{'direction':'long'}}
    assert RiskManager(RiskConfig()).correlation_rejection('B','long',positions,h,index[6]) is None
    risk=RiskManager(RiskConfig(max_pairwise_correlation=.6,correlation_lookback_bars=5))
    assert risk.correlation_rejection('B','short',positions,h,index[6]) is None
    assert risk.correlation_rejection('B','long',{},h,index[1]) is None


def test_insufficient_history_fails_closed():
    h,index=histories();risk=RiskManager(RiskConfig(max_pairwise_correlation=.6,correlation_lookback_bars=5))
    assert 'Insufficient' in risk.correlation_rejection('B','long',{'A':{'direction':'long'}},h,index[2])


def test_engine_rejects_same_batch_correlated_entry_but_keeps_exit():
    h,index=histories();frames={}
    for symbol,close in h.items():
        f=pd.DataFrame({'open':close,'high':close*1.01,'low':close*.99,'close':close,'volume':1000000,'signal':'flat','signal_score':2 if symbol=='A' else 1,'atr_14':1.},index=index)
        f.loc[index[6],'signal']='long';frames[symbol]=f.loc[index[6]:]
    engine=BacktestEngine(BacktestConfig(use_atr_stop=False,exit_on_state_change=False,exit_on_signal_flip=True),RiskManager(RiskConfig(max_pairwise_correlation=.6,correlation_lookback_bars=5)),FeatureConfig())
    result=engine.run(frames,{s:UniverseMember(s,AssetClass.EQUITY) for s in frames},100000,risk_history_by_symbol=h)
    assert set(result.trades.symbol)=={'A'}
    assert result.trades.iloc[0].exit_reason=='signal_flip'
    assert result.orders.notes.fillna('').str.contains('Exposure correlation').any()


@pytest.mark.parametrize('fields',[{'max_pairwise_correlation':1.1},{'max_pairwise_correlation':-.1},{'correlation_lookback_bars':1}])
def test_invalid_correlation_config_rejected(fields):
    with pytest.raises(ValidationError):RiskConfig(**fields)
