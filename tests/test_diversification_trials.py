import numpy as np
import pandas as pd
import pytest
from test_relative_value import frames,forced
from trader_engine.research.relative_value import RelativeValueConfig
from trader_engine.research.daily_relative_value import run_daily_relative_value
from trader_engine.research.diversification_trials import prepare_inputs,run_batch,correlation_to

@pytest.mark.parametrize('cost',[1,2])
@pytest.mark.parametrize('threshold',[0.,2.])
def test_batch_matches_event_engine(cost,threshold):
    f,d=frames(35)
    f['A']['close']*=1+np.sin(np.arange(len(d)))*.003
    fc=forced(d,tuple(range(34)));fc.prediction=np.sin(np.arange(34))*.003;fc['mean_prediction_se']=.0001
    c=RelativeValueConfig(uncertainty_multiple=threshold,pair_gross=.15,one_way_cost_bps=7*cost,annual_borrow_rate=.03*cost)
    params=pd.DataFrame([dict(candidate_id=0,forecast_id=0,pair_mask=1,uncertainty=threshold,cost_buffer=1.5,pair_gross=.15,max_positions=1)])
    inp=prepare_inputs(f,d,[['A','B']],[fc]);m,r,t,days=run_batch(inp,params,d[1],d[-1],cost_multiple=cost)
    ref=run_daily_relative_value(f,d,[['A','B']],fc,c,d[1],d[-1])
    assert m.net_pnl.iloc[0]==pytest.approx(ref.metrics['net_pnl'],abs=1e-7)
    assert m.trades.iloc[0]==ref.metrics['trades']
    np.testing.assert_allclose(r[0],ref.equity.daily_return,atol=1e-14)
    np.testing.assert_array_equal(t[0],ref.equity.trades)

def test_constant_returns_do_not_claim_zero_correlation():
    assert np.isnan(correlation_to(np.zeros((1,10)),np.arange(10))).all()

def test_future_labels_are_rejected():
    f,d=frames(5);fc=forced(d);fc.last_training_label_at=d[1]
    with pytest.raises(ValueError,match='Future'):prepare_inputs(f,d,[['A','B']],[fc])

@pytest.mark.parametrize('loss',[False,True])
def test_batch_multiple_pairs_position_caps_and_halt(loss):
    base,d=frames(15);f={};pairs=[['A','B'],['C','D'],['E','F']];forecasts=[]
    for i,(a,b) in enumerate(pairs):
        f[a]=base['A'].copy();f[b]=base['B'].copy()
        f[a]['close']*=.5 if loss else 1.005*(1+i*.001)
        f[a]['low']=np.minimum(f[a]['low'],f[a]['close']*.99)
        f[a]['high']=np.maximum(f[a]['high'],f[a]['close']*1.01)
        fc=forced(d,tuple(range(14)));fc['a']=a;fc['b']=b;fc['pair']=a+'/'+b;fc['prediction']=.03-i*.005;forecasts.append(fc)
    fc=pd.concat(forecasts,ignore_index=True)
    params=pd.DataFrame([dict(candidate_id=i,forecast_id=0,pair_mask=7,uncertainty=0.,cost_buffer=1.5,pair_gross=.2,max_positions=i+1) for i in range(3)])
    inp=prepare_inputs(f,d,pairs,[fc]);m,r,t,days=run_batch(inp,params,d[1],d[-1])
    for i in range(3):
        c=RelativeValueConfig(uncertainty_multiple=0,pair_gross=.2,max_positions=i+1)
        ref=run_daily_relative_value(f,d,pairs,fc,c,d[1],d[-1])
        np.testing.assert_allclose(r[i],ref.equity.daily_return,atol=1e-13,rtol=0)
        np.testing.assert_array_equal(t[i],ref.equity.trades)
        assert m.net_pnl.iloc[i]==pytest.approx(ref.metrics['net_pnl'],abs=1e-7)
        if loss:assert m.halted.iloc[i]
        else:assert m.trades.iloc[i]==14*(i+1)
