from importlib.util import spec_from_file_location,module_from_spec
from pathlib import Path
import numpy as np
import pandas as pd
import pytest
spec=spec_from_file_location('crypto_baseline',Path(__file__).resolve().parents[1]/'scripts/run_crypto_baseline.py')
runner=module_from_spec(spec);spec.loader.exec_module(runner)

def bars(n=230):
    close=np.arange(n,dtype=float)+100
    return pd.DataFrame(dict(open=close,high=close+1,low=close-1,close=close,volume=1000.),index=pd.date_range('2023-01-01',periods=n,tz='UTC'))

def test_future_prices_cannot_change_past_signals():
    f=bars();changed=f.copy();changed.iloc[210:,changed.columns.get_loc('close')]*=10
    pd.testing.assert_series_equal(runner.signal_frame(f).signal.iloc[:210],runner.signal_frame(changed).signal.iloc[:210])
    assert (runner.signal_frame(f).signal.iloc[:199]=='flat').all()
    assert runner.signal_frame(f).signal.iloc[199]=='long'

def test_reject_missing_daily_bar(tmp_path):
    p=tmp_path/'gapped.parquet';bars().drop(bars().index[10]).to_parquet(p)
    with pytest.raises(ValueError,match='Missing daily bars'):runner.read_bars(p)

def test_next_open_costs_and_terminal_cash_reconcile():
    f=runner.signal_frame(bars()).iloc[200:]
    config,risk=runner.configs(25,15)
    result=runner.BacktestEngine(config,runner.RiskManager(risk),runner.FeatureConfig()).run({'BTC-USD':f},{'BTC-USD':runner.UniverseMember('BTC-USD',runner.AssetClass.CRYPTO)},100000)
    assert result.trades.iloc[0].entry_timestamp==f.index[1]
    assert result.trades.iloc[0].exit_timestamp==f.index[-1]
    assert result.equity_curve.iloc[0].equity==100000
    ledger=runner.reconcile(result,{'BTC-USD':f},25,15)
    assert ledger.iloc[-1].fees>0 and ledger.iloc[-1].impact>0
    assert ledger.iloc[-1].equity==pytest.approx(100000+result.trades.pnl.sum())
    assert ledger.iloc[-1].exposure==pytest.approx(0)
    result.equity_curve.iloc[2,result.equity_curve.columns.get_loc('cash')]+=1
    with pytest.raises(AssertionError):runner.reconcile(result,{'BTC-USD':f},25,15)
