"""Batched daily research trials, equivalent to the event-based two-leg simulator.

Only disjoint pairs are supported. Prices are adjusted research units, not fills.
"""
import numpy as np
import pandas as pd
from trader_engine.research.relative_value import validate_inputs


def prepare_inputs(frames,calendar,pairs,forecasts):
    dates=validate_inputs(frames,calendar,pairs)
    if len({s for p in pairs for s in p})!=2*len(pairs):raise ValueError('Batch engine requires disjoint pairs')
    if pairs!=sorted(pairs):raise ValueError('Pairs must use alphabetical tie-break ordering')
    n=len(dates);k=len(forecasts);p=len(pairs)
    pred=np.full((k,n,p),np.nan);se=pred.copy();beta=pred.copy()
    for ki,fc in enumerate(forecasts):
        if fc.duplicated(['signal_at','pair']).any():raise ValueError('Duplicate forecasts')
        if (fc.last_training_label_at.isna()|(fc.last_training_label_at>fc.signal_at)).any():raise ValueError('Future or missing training labels')
        for pi,(a,b) in enumerate(pairs):
            g=fc.loc[fc.pair==a+'/'+b].set_index('signal_at').reindex(dates)
            # Shift signal to the following session, including the first YTD entry.
            pred[ki,1:,pi]=g.prediction.to_numpy()[:-1]
            se[ki,1:,pi]=g.mean_prediction_se.to_numpy()[:-1]
            beta[ki,1:,pi]=g.beta.to_numpy()[:-1]
    valid=np.isfinite(pred)&np.isfinite(se)&np.isfinite(beta)
    if np.any(valid&((se<0)|(beta<.5)|(beta>1.5))):raise ValueError('Invalid forecast values')
    ra=np.column_stack([(frames[a].close/frames[a].open-1).values for a,b in pairs])
    rb=np.column_stack([(frames[b].close/frames[b].open-1).values for a,b in pairs])
    return dict(dates=dates,pairs=pairs,pred=pred,se=se,beta=beta,valid=valid,ra=ra,rb=rb)


def run_batch(inputs,candidates,start,end,*,cost_multiple=1,initial_capital=100000.):
    if cost_multiple not in (1,2):raise ValueError('Only frozen base and stress costs supported')
    dates=inputs['dates'];idx=np.flatnonzero((dates>=pd.Timestamp(start))&(dates<=pd.Timestamp(end)))
    if len(idx)<2:raise ValueError('At least two sessions required')
    n=len(candidates);p=len(inputs['pairs']);rows=np.arange(n);choice=candidates.forecast_id.to_numpy(dtype=int)
    masks=candidates.pair_mask.to_numpy(dtype=int);enabled=(masks[:,None]&(1<<np.arange(p)))!=0
    uncertainty=candidates.uncertainty.to_numpy()[:,None];buffer=candidates.cost_buffer.to_numpy()[:,None]
    alloc=candidates.pair_gross.to_numpy();maxpos=candidates.max_positions.to_numpy();rate=.0007*cost_multiple;borrow_rate=.03*cost_multiple
    equity=np.full(n,initial_capital);peak=equity.copy();halted=np.zeros(n,dtype=bool);dd=np.zeros(n)
    counts=np.zeros(n,dtype=int);gross_sum=np.zeros(n);fee_sum=np.zeros(n);borrow_sum=np.zeros(n)
    returns=np.zeros((n,len(idx)));trade_counts=np.zeros((n,len(idx)),dtype=np.int16)
    for t,di in enumerate(idx):
        pred=inputs['pred'][choice,di,:];se=inputs['se'][choice,di,:];beta=inputs['beta'][choice,di,:]
        valid=inputs['valid'][choice,di,:]&enabled
        direction=np.where(pred>=0,1.,-1.);short_weight=np.where(direction>0,beta,1)/(1+beta)
        edge=np.abs(pred)-uncertainty*se-buffer*(2*rate+short_weight*borrow_rate/365)
        edge=np.where(valid&np.isfinite(edge),edge,-np.inf)
        order=np.argsort(-edge,axis=1,kind='stable')
        gross=np.zeros(n);entry_fees=np.zeros(n);pnl=np.zeros(n);entered=np.zeros(n,dtype=int)
        for rank in range(p):
            pi=order[:,rank];selected_edge=edge[rows,pi]
            eligible=(selected_edge>0)&(~halted)&(entered<maxpos)
            opening=equity-entry_fees
            notional=np.minimum(opening*alloc/(1+alloc*rate),np.maximum(0,.5*opening-gross)/(1+.5*rate))
            notional=np.where(eligible,np.maximum(0,notional),0.)
            active=notional>0
            # Use safe values for invalid rows; zero-sized invalid forecasts add zero.
            b=np.where(active,beta[rows,pi],1.);side=direction[rows,pi]
            ra=inputs['ra'][di,pi];rb=inputs['rb'][di,pi]
            gp=notional*side*(ra-b*rb)/(1+b)
            fee=notional*rate*(1+((1+ra)+b*(1+rb))/(1+b))
            borrow=notional*np.where(side>0,b,1)/(1+b)*borrow_rate/365
            pnl+=gp-fee-borrow;gross+=notional;entry_fees+=notional*rate;entered+=active
            gross_sum+=gp;fee_sum+=fee;borrow_sum+=borrow
        returns[:,t]=pnl/equity;equity+=pnl;counts+=entered;trade_counts[:,t]=entered
        peak=np.maximum(peak,equity);dd=np.minimum(dd,equity/peak-1);halted|=equity<=peak*.95
    net=equity-initial_capital
    if not np.allclose(net,gross_sum-fee_sum-borrow_sum,atol=1e-6,rtol=0):raise AssertionError('Batch account mismatch')
    metrics=pd.DataFrame(dict(candidate_id=candidates.candidate_id.values,cost_multiple=cost_multiple,net_pnl=net,total_return=net/initial_capital,trades=counts,active_days=(trade_counts>0).sum(axis=1),winning_days=(returns>0).sum(axis=1),losing_days=(returns<0).sum(axis=1),one_percent_days=(returns>=.01).sum(axis=1),max_drawdown=dd,gross_pnl=gross_sum,trading_costs=fee_sum,borrow_cost=borrow_sum,halted=halted))
    return metrics,returns,trade_counts,dates[idx]


def correlation_to(returns,benchmark):
    x=returns-returns.mean(axis=1,keepdims=True);y=benchmark-benchmark.mean();den=np.sqrt((x*x).sum(axis=1)*(y@y))
    return np.divide(x@y,den,out=np.full(len(x),np.nan),where=den>1e-18)
