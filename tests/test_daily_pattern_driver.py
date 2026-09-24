"""Accounting checks for the fixed offline daily campaign."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import pandas as pd
import pytest

spec=importlib.util.spec_from_file_location('daily_pattern_driver',Path(__file__).resolve().parents[1]/'scripts/run_daily_pattern_research.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

def result():
    return SimpleNamespace(
        equity_curve=pd.DataFrame({'equity':[99990.,100090.],'cash':[89990.,89990.],
            'gross_exposure':[10000.,10100.],'dividend_receivable':[0.,0.],
            'cumulative_gross_pnl':[0.,100.],'cumulative_impact_cost':[10.,10.],
            'cumulative_fees':[0.,0.]}),
        decisions=pd.DataFrame([{'action':'entry','quantity':100.,'raw_price':100.}]),
        trades=pd.DataFrame(),symbol_contributions=pd.DataFrame({'net_pnl':[90.]}),
        metrics={'final_equity':100090.})

def test_same_fill_gross_retains_initial_purchase_cost_and_open_pnl():
    summary=module.summarize(result())
    assert summary['gross_return_same_fills']==pytest.approx(.001)
    assert summary['net_return']==pytest.approx(.0009)
    assert summary['max_drawdown']==pytest.approx(.0001)
    assert summary['mean_closed_episode_net_pnl'] is None
    assert summary['mean_daily_gross_return_same_fills']==pytest.approx(.0005)

@pytest.mark.parametrize('column',['cash','cumulative_gross_pnl'])
def test_inconsistent_daily_accounting_rejects(column):
    broken=result();broken.equity_curve.loc[0,column]+=1
    with pytest.raises(AssertionError,match='identity'):
        module.summarize(broken)


def test_open_position_session_dates_serialize_without_losing_state(tmp_path):
    from datetime import date
    import json
    target=tmp_path/'positions.json'
    module.write_json(target,{'SPY':{'entry_session':date(2020,1,2),'qty':3.}})
    assert json.loads(target.read_text())=={'SPY':{'entry_session':'2020-01-02','qty':3.}}
