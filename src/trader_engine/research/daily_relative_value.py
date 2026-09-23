"""Daily open-to-close proxy simulation; no intraday path or broker execution."""
import math
import numpy as np
import pandas as pd
from trader_engine.research.relative_value import validate_inputs,RelativeValueResult


def run_daily_relative_value(frames,calendar,pairs,forecasts,c,start,end,*,require_daily_trade=False):
    dates=validate_inputs(frames,calendar,pairs)
    days=dates[(dates>=pd.Timestamp(start))&(dates<=pd.Timestamp(end))]
    if len(days)<2:raise ValueError('At least two sessions required')
    if forecasts.duplicated(['signal_at','pair']).any():raise ValueError('Duplicate forecasts')
    signals={d:g for d,g in forecasts.groupby('signal_at')};known={tuple(p) for p in pairs}
    cash=c.initial_capital;peak=cash;halted=False;trades=[];daily=[];decisions=[];rate=c.one_way_cost_bps/10000
    for day in days:
        i=dates.get_loc(day);previous=dates[i-1] if i else None;begin=cash;ranked=[]
        for row in signals.get(previous,pd.DataFrame()).itertuples():
            if (row.a,row.b) not in known or row.pair!=row.a+'/'+row.b:raise ValueError('Unknown forecast pair')
            if pd.isna(row.last_training_label_at) or row.last_training_label_at>previous:raise ValueError('Future training label')
            if not all(math.isfinite(v) for v in [row.prediction,row.mean_prediction_se,row.beta]) or row.mean_prediction_se<0 or not .5<=row.beta<=1.5:raise ValueError('Invalid forecast')
            direction=1 if row.prediction>=0 else -1
            short_weight=row.beta/(1+row.beta) if direction>0 else 1/(1+row.beta)
            # Conservative minimum one-day borrow charge, including same-day shorts.
            edge=abs(row.prediction)-c.uncertainty_multiple*row.mean_prediction_se-c.cost_buffer*(2*rate+short_weight*c.annual_borrow_rate/365)
            ranked.append(row._asdict()|dict(edge=edge,direction=direction))
        ranked.sort(key=lambda r:(-r['edge'],r['pair']))
        eligible=ranked[:1] if require_daily_trade else [r for r in ranked if r['edge']>0]
        gross=0.;fees=0.;borrow=0.;pnl=0.;used=set();count=0
        for r in eligible:
            if halted:break
            a,b=r['a'],r['b']
            if count>=c.max_positions or a in used or b in used:continue
            # Size all simultaneous opening orders without using today's closes/P&L.
            opening_equity=begin-fees
            notional=min(opening_equity*c.pair_gross/(1+c.pair_gross*rate),max(0,c.max_gross*opening_equity-gross)/(1+c.max_gross*rate))
            if notional<=0:continue
            oa=float(frames[a].loc[day,'open']);ob=float(frames[b].loc[day,'open'])
            ca=float(frames[a].loc[day,'close']);cb=float(frames[b].loc[day,'close'])
            qa=r['direction']*notional/(1+r['beta'])/oa;qb=-r['direction']*notional*r['beta']/(1+r['beta'])/ob
            gross_pnl=qa*(ca-oa)+qb*(cb-ob);entry_fee=notional*rate;exit_fee=(abs(qa)*ca+abs(qb)*cb)*rate
            borrow_fee=-(min(qa,0)*oa+min(qb,0)*ob)*c.annual_borrow_rate/365
            net=gross_pnl-entry_fee-exit_fee-borrow_fee
            trades.append(dict(pair=r['pair'],signal_at=previous,entry_at=day,exit_at=day,direction=r['direction'],beta=r['beta'],prediction=r['prediction'],edge=r['edge'],quantity_a=qa,quantity_b=qb,entry_gross=notional,gross_pnl=gross_pnl,trading_costs=entry_fee+exit_fee,borrow_cost=borrow_fee,net_pnl=net,exit_reason='session_close'))
            gross+=notional;fees+=entry_fee;borrow+=borrow_fee;pnl+=net;used.update([a,b]);count+=1
        cash=begin+pnl;peak=max(peak,cash)
        if cash<=peak*(1-c.account_drawdown_limit):halted=True
        daily.append(dict(date=day,starting_equity=begin,equity=cash,net_pnl=pnl,daily_return=cash/begin-1,trades=count,opening_gross_exposure=gross/(begin-fees) if begin>fees else 0,open_pairs_at_close=0,drawdown_halted=halted))
        decisions.append(dict(date=day,available_forecasts=len(ranked),cost_eligible=sum(r['edge']>0 for r in ranked),trades=count,reason='traded' if count else 'drawdown_halt' if halted else 'no_forecast' if not ranked else 'below_cost_threshold'))
    curve=pd.DataFrame(daily).set_index('date')
    columns=['pair','signal_at','entry_at','exit_at','direction','beta','prediction','edge','quantity_a','quantity_b','entry_gross','gross_pnl','trading_costs','borrow_cost','net_pnl','exit_reason']
    tr=pd.DataFrame(trades,columns=columns);path=np.r_[c.initial_capital,curve.equity.values]
    if not np.isclose(cash-c.initial_capital,tr.net_pnl.sum(),atol=1e-7,rtol=0):raise AssertionError('Account mismatch')
    metrics=dict(initial_capital=c.initial_capital,final_equity=cash,net_pnl=cash-c.initial_capital,total_return=cash/c.initial_capital-1,trading_sessions=len(days),trades=len(tr),active_days=int((curve.trades>0).sum()),winning_days=int((curve.net_pnl>0).sum()),losing_days=int((curve.net_pnl<0).sum()),flat_days=int((curve.net_pnl==0).sum()),one_percent_days=int((curve.daily_return>=.01).sum()),max_drawdown=float((path/np.maximum.accumulate(path)-1).min()),best_day=float(curve.daily_return.max()),worst_day=float(curve.daily_return.min()),gross_pnl=float(tr.gross_pnl.sum()),trading_costs=float(tr.trading_costs.sum()),borrow_cost=float(tr.borrow_cost.sum()),approved_for_trading=False)
    return RelativeValueResult(curve,tr,pd.DataFrame(decisions),metrics)
