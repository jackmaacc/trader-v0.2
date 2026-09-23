import pytest
from pydantic import ValidationError
from trader_engine.core.config import RiskConfig,RiskOverrideConfig
from trader_engine.risk.engine import RiskManager

@pytest.mark.parametrize('atr',[.5,2,6,10,40])
def test_modeled_stop_risk_cap(atr):
 r=RiskManager(RiskConfig(max_position_pct=.1,stop_atr_multiple=4,max_trade_risk_pct=.0025))
 dollars=r.target_notional(100000,100,atr)
 assert dollars<=10000
 assert dollars/100*abs(100-r.stop_price(100,atr,True))<=250+1e-9
 assert dollars/100*abs(100-r.stop_price(100,atr,False))<=250+1e-9

def test_default_preserves_notional():
 assert RiskManager(RiskConfig(max_position_pct=.1)).target_notional(100000,100,6)==10000

def test_volatility_floor_does_not_override_stop_budget():
 r=RiskManager(RiskConfig(max_position_pct=.1,volatility_target=.00001,stop_atr_multiple=4,max_trade_risk_pct=.0001))
 assert r.target_notional(100000,100,10)==pytest.approx(25)

@pytest.mark.parametrize('atr',[None,0,-1])
def test_fallback_matches_stop(atr):
 r=RiskManager(RiskConfig(max_position_pct=.1,stop_atr_multiple=4,max_trade_risk_pct=.0025))
 assert r.target_notional(100000,100,atr)==pytest.approx(3125)

@pytest.mark.parametrize('atr',[float('nan'),float('inf')])
def test_nonfinite_fails_closed(atr):
 assert RiskManager(RiskConfig(max_trade_risk_pct=.0025)).target_notional(100000,100,atr)==0

@pytest.mark.parametrize('cls',[RiskConfig,RiskOverrideConfig])
@pytest.mark.parametrize('value',[0,-.01,1.01])
def test_invalid_budget(cls,value):
 with pytest.raises(ValidationError):cls(max_trade_risk_pct=value)
