"""Strict, causal minute-bar simulator. Research only: no broker connectivity."""
from dataclasses import dataclass
from math import floor,isfinite
import numpy as np
import pandas as pd
from pydantic import BaseModel,ConfigDict,Field
from trader_engine.data.base import validate_bars

class IntradayConfig(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    initial_capital:float=Field(100000,gt=0)
    opening_minutes:int=Field(15,ge=1)
    volume_lookback:int=Field(20,ge=2)
    relative_volume:float=Field(1.5,gt=0)
    min_minute_dollars:float=Field(250000,ge=0)
    min_price:float=Field(5,gt=0)
    execution_delay_minutes:int=Field(2,ge=1)
    atr_multiple:float=Field(1.5,gt=0)
    reward_risk:float=Field(2,gt=0)
    min_reward_cost_multiple:float=Field(3,ge=1)
    risk_per_trade:float=Field(.001,gt=0,le=1)
    portfolio_stop_risk:float=Field(.005,gt=0,le=1)
    max_position:float=Field(.1,gt=0,le=1)
    max_gross:float=Field(.5,gt=0,le=1)
    max_correlation:float|None=Field(.6,ge=0,le=1)
    correlation_minutes:int=Field(30,ge=2)
    max_positions:int=Field(5,ge=1)
    daily_loss_limit:float=Field(.01,gt=0,le=1)
    max_entries_per_day:int=Field(50,ge=1)
    max_entries_per_symbol:int=Field(3,ge=1)
    cooldown_minutes:int=Field(5,ge=0)
    max_hold_minutes:int=Field(30,ge=1)
    flatten_minutes:int=Field(5,ge=1)
    participation:float=Field(.01,gt=0,le=.1)
    one_way_cost_bps:float=Field(7,ge=0,lt=10000)

@dataclass
class IntradayResult:
    equity:pd.DataFrame
    daily:pd.DataFrame
    trades:pd.DataFrame
    decisions:pd.DataFrame
    metrics:dict


def validate_schedule(schedule):
    if schedule.empty or not {'open','close'}.issubset(schedule):raise ValueError('A nonempty exchange calendar is required')
    result=schedule.copy()
    for key in ['open','close']:
        parsed=[pd.Timestamp(v) for v in result[key]]
        if any(t.tzinfo is None for t in parsed):raise ValueError('Calendar timestamps must be timezone-aware')
        result[key]=pd.to_datetime(parsed,utc=True)
    if result.open.duplicated().any() or not result.open.is_monotonic_increasing:raise ValueError('Calendar must be unique and ordered')
    for row in result.itertuples():
        if row.open!=row.open.floor('min') or row.close!=row.close.floor('min') or not pd.Timedelta(minutes=30)<=row.close-row.open<=pd.Timedelta(hours=12):raise ValueError('Invalid session interval')
    if len(result)>1 and (result.open.iloc[1:].reset_index(drop=True)<result.close.iloc[:-1].reset_index(drop=True)).any():raise ValueError('Overlapping sessions')
    return result


def validate_minutes(frame):
    validate_bars(frame)
    if frame.index.tz is None or not frame.index.is_monotonic_increasing:raise ValueError('Minute bars must be timezone-aware and chronological')
    if not (frame.index==frame.index.floor('min')).all():raise ValueError('Expected start-of-minute timestamps')


def audit_coverage(frames,schedule):
    schedule=validate_schedule(schedule);issues=[]
    if not frames:raise ValueError('At least one symbol is required')
    for symbol,f in frames.items():
        validate_minutes(f)
        for s in schedule.itertuples():
            expected=pd.date_range(s.open,s.close,freq='min',inclusive='left');missing=expected.difference(f.index)
            if len(missing):issues.append(dict(symbol=symbol,session=str(s.open.date()),missing_minutes=len(missing),first_missing=str(missing[0])))
    return pd.DataFrame(issues,columns=['symbol','session','missing_minutes','first_missing'])


def signals(frame,c):
    close=frame.close;typical=(frame.high+frame.low+close)/3
    vwap=(typical*frame.volume).cumsum()/frame.volume.cumsum().replace(0,np.nan)
    fast=close.ewm(span=9,adjust=False).mean();slow=close.ewm(span=21,adjust=False).mean()
    prior_volume=frame.volume.rolling(c.volume_lookback).mean().shift(1)
    rv=frame.volume/prior_volume.replace(0,np.nan)
    opening=frame.high.iloc[:c.opening_minutes].cummax().reindex(frame.index).ffill()
    tr=pd.concat([frame.high-frame.low,(frame.high-close.shift()).abs(),(frame.low-close.shift()).abs()],axis=1).max(axis=1)
    atr=tr.rolling(14,min_periods=14).mean()
    breakout=(close>opening)&(close.shift()<=opening.shift())
    recovery=(close>vwap)&(close.shift()<=vwap.shift())&(fast>fast.shift())
    ready=(np.arange(len(frame))>=max(c.opening_minutes,c.volume_lookback))
    eligible=ready&(close>=c.min_price)&(close*frame.volume>=c.min_minute_dollars)&(rv>=c.relative_volume)&(fast>slow)&(close>vwap)&(atr>0)
    return pd.DataFrame({'entry':eligible&(breakout|recovery),'setup':np.where(breakout,'opening_breakout','vwap_recovery'),'atr':atr,'score':rv,'vwap':vwap},index=frame.index)


def run_intraday(frames,schedule,c=IntradayConfig(),*,signal_builder=signals):
    """Costs are explicit cash charges; no assumed quote or queue priority."""
    schedule=validate_schedule(schedule);issues=audit_coverage(frames,schedule)
    if not issues.empty:raise ValueError(f'Missing minute coverage in {len(issues)} symbol-sessions; run audit_coverage and repair data before testing')
    cash=c.initial_capital;positions={};trades=[];decisions=[];equity_rows=[];daily=[];cost=c.one_way_cost_bps/10000
    for session in schedule.itertuples():
        start_equity=cash;halted=False;pending={};counts={};cooldown={};entries=0
        idx=pd.date_range(session.open,session.close,freq='min',inclusive='left');flat_at=session.close-pd.Timedelta(minutes=c.flatten_minutes)
        if flat_at<=session.open:raise ValueError('Flatten window consumes whole session')
        data={s:f.reindex(idx) for s,f in frames.items()};sig={s:signal_builder(f,c) for s,f in data.items()}
        bars={s:list(f.itertuples()) for s,f in data.items()};signal_rows={s:list(f.itertuples()) for s,f in sig.items()}
        def value(i,field):return cash+sum(p['quantity']*getattr(bars[s][i],field) for s,p in positions.items())
        def close_position(symbol,stamp,raw_price,reason):
            nonlocal cash
            p=positions.pop(symbol);exit_value=p['quantity']*raw_price;fee=exit_value*cost;cash+=exit_value-fee
            pnl=exit_value-p['entry_notional']-p['entry_cost']-fee
            trades.append(dict(symbol=symbol,signal_at=p['signal_at'],entry_at=p['entry_at'],exit_at=stamp,setup=p['setup'],quantity=p['quantity'],entry_price=p['entry_price'],exit_price=raw_price,pnl=pnl,costs=p['entry_cost']+fee,initial_risk=p['initial_risk'],exit_reason=reason))
            cooldown[symbol]=stamp+pd.Timedelta(minutes=c.cooldown_minutes)
        for i,t in enumerate(idx):
            # Opening mark is observed before any entries; a loss breach latches all day.
            if value(i,'open')<=start_equity*(1-c.daily_loss_limit):halted=True
            for s,p in list(positions.items()):
                b=bars[s][i];reason=None;price=b.open
                if halted:reason='daily_loss_halt'
                elif t>=flat_at:reason='session_flatten'
                elif b.open<=p['stop']:reason='gap_stop'
                elif b.open>=p['target']:reason='target';price=p['target']
                elif t-p['entry_at']>=pd.Timedelta(minutes=c.max_hold_minutes):reason='time_exit'
                if reason:close_position(s,t,price,reason)
            if value(i,'open')<=start_equity*(1-c.daily_loss_limit):halted=True
            due=sorted(pending.pop(t,[]),key=lambda p:(-p['score'],p['symbol']))
            for order in due:
                s=order['symbol'];b=bars[s][i];eq=value(i,'open');reason=None
                if halted or t>=flat_at:reason='session_halted_or_closing'
                elif s in positions:reason='already_held'
                elif entries>=c.max_entries_per_day or counts.get(s,0)>=c.max_entries_per_symbol:reason='entry_limit'
                elif t<cooldown.get(s,session.open):reason='cooldown'
                elif len(positions)>=c.max_positions:reason='position_limit'
                if reason is None and positions and c.max_correlation is not None:
                    for held in positions:
                        past=data[s].loc[data[s].index<order['signal_at'],'close'].tail(c.correlation_minutes+1).pct_change(fill_method=None)
                        other=data[held].loc[data[held].index<order['signal_at'],'close'].tail(c.correlation_minutes+1).pct_change(fill_method=None)
                        paired=pd.concat([past,other],axis=1).dropna()
                        correlation=paired.iloc[:,0].corr(paired.iloc[:,1]) if len(paired)>=c.correlation_minutes and (paired.std()>0).all() else float('nan')
                        if not isfinite(correlation) or correlation>c.max_correlation:
                            reason='correlation_or_insufficient_history';break
                distance=order['atr']*c.atr_multiple
                if reason is None and (not isfinite(distance) or distance<=0 or b.open-distance<=0):reason='invalid_stop'
                if reason is None and distance*c.reward_risk<2*b.open*cost*c.min_reward_cost_multiple:reason='insufficient_reward_after_costs'
                if reason is not None:decisions.append(dict(timestamp=t,symbol=s,status='rejected',reason=reason));continue
                gross=sum(p['quantity']*bars[k][i].open for k,p in positions.items())
                committed=sum(p['quantity']*max(bars[k][i].open-p['stop'],0) for k,p in positions.items())
                limits=[eq*c.risk_per_trade/distance,eq*c.max_position/(b.open*(1+c.max_position*cost)),max(0,(c.max_gross*eq-gross))/(b.open*(1+c.max_gross*cost)),max(0,c.portfolio_stop_risk*eq-committed)/(distance+c.portfolio_stop_risk*b.open*cost),cash/(b.open*(1+cost)),order['prior_volume']*c.participation]
                qty=max(0,floor(min(limits)))
                if qty<1:decisions.append(dict(timestamp=t,symbol=s,status='rejected',reason='risk_cash_or_participation_limit'));continue
                notional=qty*b.open;fee=notional*cost;cash-=notional+fee
                positions[s]=dict(quantity=qty,entry_at=t,signal_at=order['signal_at'],entry_price=b.open,entry_notional=notional,entry_cost=fee,stop=b.open-distance,target=b.open+distance*c.reward_risk,initial_risk=qty*distance,setup=order['setup'])
                entries+=1;counts[s]=counts.get(s,0)+1;decisions.append(dict(timestamp=t,symbol=s,status='filled',reason=order['setup']))
                assert cash>=-1e-7
                if value(i,'open')<=start_equity*(1-c.daily_loss_limit):halted=True
            for s,p in list(positions.items()):
                b=bars[s][i]
                # Unknown intrabar ordering: always give the stop priority.
                if b.low<=p['stop']:close_position(s,t+pd.Timedelta(minutes=1),p['stop'],'stop')
                elif b.high>=p['target']:close_position(s,t+pd.Timedelta(minutes=1),p['target'],'target')
            eq=value(i,'close')
            if eq<=start_equity*(1-c.daily_loss_limit):halted=True
            equity_rows.append(dict(timestamp=t+pd.Timedelta(minutes=1),equity=eq,cash=cash,open_positions=len(positions),session_halted=halted))
            if not halted and t+pd.Timedelta(minutes=c.execution_delay_minutes)<flat_at:
                for s in data:
                    row=signal_rows[s][i]
                    if row.entry:
                        target=t+pd.Timedelta(minutes=c.execution_delay_minutes)
                        pending.setdefault(target,[]).append(dict(symbol=s,signal_at=t+pd.Timedelta(minutes=1),atr=row.atr,score=row.score,setup=row.setup,prior_volume=bars[s][i].volume))
        assert not positions,'No overnight positions permitted'
        daily.append(dict(session=str(session.open.date()),starting_equity=start_equity,equity=cash,net_pnl=cash-start_equity,return_pct=cash/start_equity-1,entries=entries,halted=halted))
    eq=pd.DataFrame(equity_rows).set_index('timestamp');daily=pd.DataFrame(daily);tr=pd.DataFrame(trades,columns=['symbol','signal_at','entry_at','exit_at','setup','quantity','entry_price','exit_price','pnl','costs','initial_risk','exit_reason'])
    assert abs(cash-c.initial_capital-tr.pnl.sum())<1e-6
    daily_path=np.r_[c.initial_capital,daily.equity.to_numpy()];minute_path=np.r_[c.initial_capital,eq.equity.to_numpy()];wins=float(tr.loc[tr.pnl>0,'pnl'].sum());losses=float(tr.loc[tr.pnl<0,'pnl'].sum())
    metrics=dict(initial_capital=c.initial_capital,ending_equity=cash,net_pnl=cash-c.initial_capital,total_return=cash/c.initial_capital-1,trades=len(tr),win_rate=float((tr.pnl>0).mean()) if len(tr) else 0,net_expectancy_dollars=float(tr.pnl.mean()) if len(tr) else 0,winning_pnl=wins,losing_pnl=losses,profit_factor=wins/abs(losses) if losses else None,total_costs=float(tr.costs.sum()),daily_close_drawdown=float((daily_path/np.maximum.accumulate(daily_path)-1).min()),intraday_close_drawdown=float((minute_path/np.maximum.accumulate(minute_path)-1).min()),sessions=len(daily),one_percent_days=int((daily.return_pct>=.01).sum()),losing_days=int((daily.return_pct<0).sum()),broker_orders_submitted=0)
    return IntradayResult(eq,daily,tr,pd.DataFrame(decisions,columns=['timestamp','symbol','status','reason']),metrics)


def intraday_benchmark(frames,schedule,c):
    """Same-session equal-dollar benchmark, same gross cap and costs; no overnight risk."""
    schedule=validate_schedule(schedule)
    if not audit_coverage(frames,schedule).empty:raise ValueError('Benchmark requires complete minute coverage')
    cash=c.initial_capital;cost=c.one_way_cost_bps/10000;rows=[]
    for session in schedule.itertuples():
        start=cash;allocation=start*c.max_gross/(len(frames)*(1+cost))
        close_at=session.close-pd.Timedelta(minutes=c.flatten_minutes)
        for f in frames.values():
            opening=float(f.loc[session.open,'open']);exit_price=float(f.loc[close_at,'open']);qty=floor(allocation/opening)
            cash+=qty*(exit_price*(1-cost)-opening*(1+cost))
        rows.append(dict(session=str(session.open.date()),equity=cash,net_pnl=cash-start,return_pct=cash/start-1))
    return pd.DataFrame(rows)
