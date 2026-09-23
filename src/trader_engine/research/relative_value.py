"""Causal relative-value research; adjusted units, not executable broker fills."""
from dataclasses import dataclass
from math import isfinite
import numpy as np
import pandas as pd
from pydantic import BaseModel,ConfigDict,Field
from trader_engine.data.base import validate_bars

class RelativeValueConfig(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    initial_capital:float=Field(100000,gt=0)
    feature_lookback:int=Field(60,ge=10)
    training_lookback:int=Field(252,ge=20)
    minimum_labels:int=Field(120,ge=10)
    hold_sessions:int=Field(5,ge=1)
    uncertainty_multiple:float=Field(2,ge=0)
    cost_buffer:float=Field(1.5,ge=1)
    one_way_cost_bps:float=Field(7,ge=0,lt=1000)
    annual_borrow_rate:float=Field(.03,ge=0,le=1)
    pair_gross:float=Field(.1,gt=0,le=1)
    max_gross:float=Field(.5,gt=0,le=1)
    max_positions:int=Field(3,ge=1)
    pair_loss_limit:float=Field(.0025,gt=0,le=1)
    daily_loss_limit:float=Field(.01,gt=0,le=1)
    account_drawdown_limit:float=Field(.05,gt=0,le=1)

@dataclass
class RelativeValueResult:
    equity:pd.DataFrame
    trades:pd.DataFrame
    decisions:pd.DataFrame
    metrics:dict


def validate_inputs(frames,calendar,pairs):
    dates=pd.DatetimeIndex(pd.to_datetime(calendar))
    if dates.empty or dates.hasnans or dates.has_duplicates or not dates.is_monotonic_increasing:raise ValueError('Unique ordered exchange sessions required')
    if not pairs or any(len(p)!=2 or p[0]==p[1] for p in pairs):raise ValueError('Distinct two-symbol pairs required')
    if len({tuple(sorted(p)) for p in pairs})!=len(pairs):raise ValueError('Duplicate pairs')
    for s in {s for p in pairs for s in p}:
        if s not in frames:raise ValueError('Missing symbol '+s)
        f=frames[s];validate_bars(f)
        if not f.index.is_monotonic_increasing or not f.index.equals(dates):raise ValueError('Daily prices must match the full exchange calendar exactly: '+s)
    return dates


def make_forecasts(frames,pairs,c,*,same_day_exit=False):
    if c.minimum_labels>c.training_lookback:raise ValueError('Minimum labels exceeds training window')
    rows=[]
    label_lag=1 if same_day_exit else 1+c.hold_sessions
    for a,b in pairs:
        fa,fb=frames[a],frames[b];dates=fa.index
        if not dates.equals(fb.index):raise ValueError('Pair calendars differ')
        y=np.log(fa.close.to_numpy());x=np.log(fb.close.to_numpy());oa=fa.open.to_numpy();ob=fb.open.to_numpy();n=len(dates)
        feature=np.full(n,np.nan);beta=np.full(n,np.nan);labels=np.full(n,np.nan)
        for i in range(c.feature_lookback,n):
            xx=x[i-c.feature_lookback:i];yy=y[i-c.feature_lookback:i];dx=xx-xx.mean();den=dx@dx
            if den<=1e-12:continue
            slope=float(dx@(yy-yy.mean())/den)
            if not .5<=slope<=1.5:continue
            intercept=yy.mean()-slope*xx.mean();residual=y[i]-intercept-slope*x[i]
            if np.std(yy-intercept-slope*xx,ddof=2)<=1e-10:continue
            beta[i]=slope;feature[i]=-residual/(1+slope)
            end=i+label_lag
            if end<n:
                exit_a=fa.close.iloc[end] if same_day_exit else oa[end]
                exit_b=fb.close.iloc[end] if same_day_exit else ob[end]
                labels[i]=((exit_a/oa[i+1]-1)-slope*(exit_b/ob[i+1]-1))/(1+slope)
        for i in range(n):
            if not np.isfinite(feature[i]):continue
            # Signals are formed after close; only completed outcomes are usable.
            latest=i-label_lag
            candidates=np.arange(max(0,latest-c.training_lookback+1),latest+1)
            candidates=candidates[np.isfinite(feature[candidates])&np.isfinite(labels[candidates])]
            if len(candidates)<c.minimum_labels:continue
            tx=feature[candidates];ty=labels[candidates];dx=tx-tx.mean();den=dx@dx
            if den<=1e-14:continue
            slope=float(dx@(ty-ty.mean())/den);intercept=float(ty.mean()-slope*tx.mean());pred=intercept+slope*feature[i]
            errors=ty-(intercept+slope*tx);sigma=float(np.sqrt((errors@errors)/(len(tx)-2)))
            se=sigma*np.sqrt(1/len(tx)+(feature[i]-tx.mean())**2/den)
            rows.append(dict(pair=a+'/'+b,a=a,b=b,signal_at=dates[i],feature=feature[i],beta=beta[i],prediction=pred,mean_prediction_se=se,training_labels=len(tx),last_training_label_at=dates[candidates[-1]+label_lag],label=labels[i],label_available_at=dates[i+label_lag] if i+label_lag<n else pd.NaT))
    cols=['pair','a','b','signal_at','feature','beta','prediction','mean_prediction_se','training_labels','last_training_label_at','label','label_available_at']
    return pd.DataFrame(rows,columns=cols)


def run_relative_value(frames,calendar,pairs,forecasts,c,start,end):
    dates=validate_inputs(frames,calendar,pairs);days=dates[(dates>=pd.Timestamp(start))&(dates<=pd.Timestamp(end))]
    if len(days)<2:raise ValueError('At least two evaluation sessions required')
    cash=c.initial_capital;positions={};trades=[];decisions=[];equity=[];pending=[];exits={};peak=cash;previous_equity=cash;halted=False;cost=c.one_way_cost_bps/10000
    signals={t:g for t,g in forecasts.groupby('signal_at')};date_index={d:i for i,d in enumerate(dates)}
    for day in days:
        di=date_index[day];day_halt=False
        marks={s:float(f.loc[day,'open']) for s,f in frames.items()}
        def account():return cash+sum(sum(q*marks[s] for s,q in p['quantities'].items()) for p in positions.values())
        def close_pair(key,reason):
            nonlocal cash
            p=positions.pop(key);value=sum(q*marks[s] for s,q in p['quantities'].items());turnover=sum(abs(q)*marks[s] for s,q in p['quantities'].items());fee=turnover*cost;cash+=value-fee
            gross=sum(q*(marks[s]-p['entry_prices'][s]) for s,q in p['quantities'].items());net=gross-p['entry_cost']-fee-p['borrow_cost']
            trades.append(dict(pair=key,signal_at=p['signal_at'],entry_at=p['entry_at'],exit_at=day,direction=p['direction'],beta=p['beta'],entry_gross=p['entry_gross'],gross_pnl=gross,trading_costs=p['entry_cost']+fee,borrow_cost=p['borrow_cost'],net_pnl=net,exit_reason=reason))
        # Accrue borrow on last observed short marks for actual elapsed calendar days.
        for p in positions.values():
            elapsed=(day-p['last_accrual']).days
            borrow=sum(-q*p['last_prices'][s] for s,q in p['quantities'].items() if q<0)*c.annual_borrow_rate*elapsed/365
            cash-=borrow;p['borrow_cost']+=borrow;p['last_accrual']=day
        opened_equity=account();peak=max(peak,opened_equity)
        if opened_equity<=previous_equity*(1-c.daily_loss_limit):day_halt=True
        if opened_equity<=peak*(1-c.account_drawdown_limit):halted=True
        for key,p in list(positions.items()):
            if halted or day_halt:close_pair(key,'account_halt')
            elif key in exits:close_pair(key,exits[key])
            elif di-p['entry_index']>=c.hold_sessions:close_pair(key,'horizon_exit')
        exits={}
        for order in sorted(pending,key=lambda o:(-o['edge'],o['pair'])):
            key=order['pair'];a,b=order['a'],order['b'];eq=account();gross=sum(sum(abs(q)*marks[s] for s,q in p['quantities'].items()) for p in positions.values());used={s for p in positions.values() for s in p['quantities']}
            if eq<=previous_equity*(1-c.daily_loss_limit):day_halt=True
            if eq<=peak*(1-c.account_drawdown_limit):halted=True
            reason=None
            if halted or day_halt or day==days[-1]:reason='account_halt_or_final_session'
            elif len(positions)>=c.max_positions or a in used or b in used:reason='portfolio_overlap_or_position_cap'
            elif eq<=0:reason='nonpositive_equity'
            if reason:decisions.append(dict(date=day,pair=key,status='rejected',reason=reason));continue
            notional=min(eq*c.pair_gross/(1+c.pair_gross*cost),max(0,c.max_gross*eq-gross)/(1+c.max_gross*cost))
            if notional<=0:continue
            beta=order['beta'];direction=1 if order['prediction']>0 else -1
            qa=direction*notional/(1+beta)/marks[a];qb=-direction*beta*notional/(1+beta)/marks[b];quantities={a:qa,b:qb};fee=notional*cost
            cash-=sum(q*marks[s] for s,q in quantities.items())+fee
            positions[key]=dict(quantities=quantities,entry_prices={a:marks[a],b:marks[b]},last_prices={a:marks[a],b:marks[b]},entry_at=day,signal_at=order['signal_at'],entry_index=di,entry_equity=eq,entry_gross=notional,entry_cost=fee,borrow_cost=0.,last_accrual=day,beta=beta,direction=direction)
            decisions.append(dict(date=day,pair=key,status='filled',reason='forecast_above_cost_threshold'))
            if account()<=previous_equity*(1-c.daily_loss_limit):day_halt=True
        pending=[];marks={s:float(f.loc[day,'close']) for s,f in frames.items()}
        eq=account();peak=max(peak,eq)
        if eq<=previous_equity*(1-c.daily_loss_limit):day_halt=True
        if eq<=peak*(1-c.account_drawdown_limit):halted=True
        for key,p in positions.items():
            pnl=sum(q*(marks[s]-p['entry_prices'][s]) for s,q in p['quantities'].items())-p['entry_cost']-p['borrow_cost']
            if halted or day_halt:exits[key]='account_halt'
            elif pnl<=-c.pair_loss_limit*p['entry_equity']:exits[key]='pair_loss_limit'
            p['last_prices']={s:marks[s] for s in p['quantities']}
        if day==days[-1]:
            for key in list(positions):close_pair(key,'evaluation_end')
            eq=cash
        elif not halted and not day_halt:
            for row in signals.get(day,pd.DataFrame()).itertuples():
                if pd.isna(row.last_training_label_at) or row.last_training_label_at>day:raise ValueError('Future training label detected')
                if (row.a,row.b) not in {tuple(p) for p in pairs} or row.pair!=row.a+'/'+row.b:raise ValueError('Unknown forecast pair')
                direction=1 if row.prediction>0 else -1
                short_weight=row.beta/(1+row.beta) if direction>0 else 1/(1+row.beta)
                exit_index=min(di+1+c.hold_sessions,len(dates)-1);calendar_days=(dates[exit_index]-dates[di+1]).days
                hurdle=c.cost_buffer*(2*cost+short_weight*c.annual_borrow_rate*calendar_days/365)
                edge=abs(row.prediction)-c.uncertainty_multiple*row.mean_prediction_se-hurdle
                if not all(isfinite(v) for v in [row.prediction,row.beta,row.mean_prediction_se,edge]) or row.mean_prediction_se<0 or not .5<=row.beta<=1.5:raise ValueError('Invalid forecast values')
                if edge>0:pending.append(row._asdict()|{'edge':edge})
                else:decisions.append(dict(date=day,pair=row.pair,status='filtered',reason='insufficient_forecast_after_uncertainty_and_costs'))
        gross=sum(sum(abs(q)*marks[s] for s,q in p['quantities'].items()) for p in positions.values())
        equity.append(dict(date=day,equity=eq,cash=cash,gross_exposure=gross/eq if eq>0 else float('nan'),open_pairs=len(positions),drawdown_halted=halted,daily_halted=day_halt));previous_equity=eq
    curve=pd.DataFrame(equity).set_index('date');tr=pd.DataFrame(trades,columns=['pair','signal_at','entry_at','exit_at','direction','beta','entry_gross','gross_pnl','trading_costs','borrow_cost','net_pnl','exit_reason'])
    assert abs(cash-c.initial_capital-tr.net_pnl.sum())<1e-6
    path=np.r_[c.initial_capital,curve.equity.to_numpy()];returns=path[1:]/path[:-1]-1;loss=-tr.loc[tr.net_pnl<0,'net_pnl'].sum();wins=tr.loc[tr.net_pnl>0,'net_pnl'].sum()
    metrics=dict(net_pnl=cash-c.initial_capital,total_return=cash/c.initial_capital-1,trades=len(tr),win_rate=float((tr.net_pnl>0).mean()) if len(tr) else 0,net_expectancy=float(tr.net_pnl.mean()) if len(tr) else 0,profit_factor=float(wins/loss) if loss else None,max_drawdown=float((path/np.maximum.accumulate(path)-1).min()),trading_costs=float(tr.trading_costs.sum()),borrow_cost=float(tr.borrow_cost.sum()),one_percent_days=int((returns>=.01).sum()),losing_days=int((returns<0).sum()),average_gross_exposure=float(curve.gross_exposure.mean()),approved_for_trading=False)
    return RelativeValueResult(curve,tr,pd.DataFrame(decisions,columns=['date','pair','status','reason']),metrics)
