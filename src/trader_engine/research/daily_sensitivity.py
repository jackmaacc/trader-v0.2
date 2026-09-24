"""Batched H1 replay of the shared daily ledger, never a broker execution path.

Signals use the original causal daily_feature and decision_due functions. All
scenarios keep individual cash, positions, entitlements, halts and pending orders.
Only complete five-ETF calendars are supported; missing data fail closed.
"""
from dataclasses import dataclass
import numpy as np
import pandas as pd
from trader_engine.research.strategy_spec import ETF_UNIVERSE
from trader_engine.research.daily_hypotheses import daily_feature, decision_due

SYMBOLS=tuple(ETF_UNIVERSE)
FIELDS=('equity','cash','cumulative_gross_pnl','cumulative_net_pnl','cumulative_impact_cost',
        'dividend_receivable','dividends_paid','gross_exposure','open_positions','daily_halt',
        'drawdown_halt','pending_orders','trade_count')
REASONS={1:'trend_entry',2:'trend_exit',3:'drawdown_halt',4:'daily_loss_halt',5:'protective_exit',6:'exposure_cap_reduction',7:'passive_initialization'}

@dataclass
class Prepared:
    dates:pd.DatetimeIndex
    indices:np.ndarray
    opens:np.ndarray
    closes:np.ndarray
    opening:pd.DatetimeIndex
    closing:pd.DatetimeIndex
    features:dict
    actions:list
    has_next:np.ndarray


def prepare(frames,schedule,actions,start,end):
    """Validate with shared adapter first, then cache only causal input features."""
    from trader_engine.backtest.daily_etf import run_daily_etf_replay
    run_daily_etf_replay(frames,'H1',schedule=schedule,corporate_actions=actions,start=start,end=end)
    cal=schedule.copy()
    for c in ('market_open','market_close'):cal[c]=pd.to_datetime(cal[c],utc=True)
    dates=pd.DatetimeIndex([x.date() for x in cal.market_open.dt.tz_convert('America/New_York')]);cal.index=dates
    active=dates[(dates>=pd.Timestamp(start))&(dates<=pd.Timestamp(end))]
    fs={s:f.copy() for s,f in frames.items()}
    for f in fs.values():f.index=pd.DatetimeIndex([pd.Timestamp(x).date() for x in f.index])
    if any(not active.isin(f.index).all() for f in fs.values()):raise ValueError('Batched H1 requires complete daily coverage')
    ix=dates.get_indexer(active);features={}
    for d,i in zip(active,ix):
        if i+1<len(dates) and decision_due('H1',d,dates[i+1]):
            features[d]={}
            for s in SYMBOLS:
                f=fs[s].loc[:d].copy()
                for t,a in actions[(actions.symbol==s)&(actions.index<=cal.loc[d,'market_close'])].iterrows():
                    ratio=float(a.get('split_ratio',1))
                    if ratio!=1:f.loc[f.index<pd.Timestamp(t.tz_convert('America/New_York').date()),['open','high','low','close']]/=ratio
                f['signal_scale']=1.
                features[d][s]=daily_feature('H1',f,dates,d)
    acts=[]
    for t,a in actions.iterrows():
        day=pd.Timestamp(t.tz_convert('America/New_York').date())
        if active[0]<=day<=active[-1]:
            acts.append((day,SYMBOLS.index(a.symbol),float(a.get('split_ratio',1)),float(a.get('cash_dividend',0)),a.get('payment_timestamp',pd.NaT)))
    return Prepared(active,ix,np.array([[fs[s].loc[d,'open'] for s in SYMBOLS] for d in active],float),
        np.array([[fs[s].loc[d,'close'] for s in SYMBOLS] for d in active],float),
        pd.DatetimeIndex(cal.loc[active,'market_open']),pd.DatetimeIndex(cal.loc[active,'market_close']),features,acts,ix+1<len(dates))


def run_batch(p,costs,delays,benchmark_weight=None):
    costs=np.asarray(costs,float);delays=np.asarray(delays)
    if costs.ndim!=1 or delays.shape!=costs.shape or not np.isfinite(costs).all() or ((costs<0)|(costs>=10000)).any() or not np.isin(delays,[0,1]).all():raise ValueError('Invalid scenario inputs')
    if benchmark_weight not in (None,.25,1.):raise ValueError('Unsupported benchmark weight')
    n=len(costs);impact=costs/10000;cash=np.full(n,100000.);fees=np.zeros(n);paid=np.zeros(n)
    qty=np.zeros((n,5));stop=qty.copy();basis=qty.copy();raw_basis=qty.copy();episode_net=qty.copy();episode_gross=qty.copy();episode_div=qty.copy()
    entry_i=np.full((n,5),-1,int);cooldown=entry_i.copy();due=entry_i.copy();target=qty.copy();pending_stop=qty.copy();reason=np.zeros((n,5),int)
    previous=np.full(n,100000.);peak=previous.copy();halt=np.zeros(n,bool);block=np.full(n,-1,int);trades=np.zeros(n,int)
    claims=[];events=[];daily=np.empty((n,len(p.dates),len(FIELDS)))
    sorted_symbols=sorted(range(5),key=lambda j:SYMBOLS[j]);cluster_indices=[SYMBOLS.index(s) for s in ('SPY','QQQ','IWM')]
    marks=np.zeros(5)
    def receivable():return sum((v for v,_ in claims),np.zeros(n))
    def equity():return cash+np.sum(qty*marks,axis=1)+receivable()
    def event(j,q,raw,px,r,action,epnet=None,epgross=None):
        use=np.flatnonzero(q>0)
        if len(use):
            events.append(pd.DataFrame(dict(trial_index=use,session_index=k,timestamp=p.opening[k],symbol=SYMBOLS[j],
                action=np.asarray(action)[use] if not isinstance(action,str) else action,reason=[REASONS[int(x)] for x in np.broadcast_to(r,(n,))[use]],
                quantity=q[use],raw_price=raw,price=px[use],episode_net=np.full(len(use),np.nan) if epnet is None else epnet[use],
                episode_gross=np.full(len(use),np.nan) if epgross is None else epgross[use])))
    def sell(j,amount,raw,r):
        nonlocal cash,fees
        q=np.minimum(amount,qty[:,j]);frac=np.divide(q,qty[:,j],out=np.zeros(n),where=qty[:,j]>0);px=raw*(1-impact)
        b=basis[:,j]*frac;rb=raw_basis[:,j]*frac
        cash+=q*px;fees+=q*(raw-px);episode_net[:,j]+=q*px-b;episode_gross[:,j]+=q*raw-rb
        basis[:,j]-=b;raw_basis[:,j]-=rb;qty[:,j]-=q
        closed=(q>0)&(qty[:,j]<1e-9);trades[closed]+=1
        event(j,q,raw,px,r,np.where(closed,'exit','reduce'),np.where(closed,episode_net[:,j]+episode_div[:,j],np.nan),np.where(closed,episode_gross[:,j]+episode_div[:,j],np.nan))
        cooldown[closed&(r==5),j]=i+10
        for a in (qty,stop,basis,raw_basis,episode_net,episode_gross,episode_div):a[closed,j]=0.
    def allowed(j,requested,raw,anchor,eq,cap):
        gross=np.sum(qty*marks,axis=1);cluster=np.sum(qty[:,cluster_indices]*marks[cluster_indices],axis=1)
        risk=np.sum(qty*np.maximum(marks-np.minimum(marks,stop)*(1-impact[:,None]),0),axis=1)
        px=raw*(1+impact);per_cost=px-raw;stop_cost=px-anchor*(1-impact)
        existing_risk=qty[:,j]*np.maximum(raw-np.minimum(raw,anchor)*(1-impact),0)
        safe=np.where(stop_cost>0,stop_cost,1.)
        limits=[requested,cash/px,np.maximum(0,.001*eq-existing_risk)/(safe+.001*per_cost),np.maximum(0,.005*eq-risk)/(safe+.005*per_cost),
            np.maximum(0,.25*eq-gross)/(raw+.25*per_cost),np.maximum(0,cap*eq-qty[:,j]*raw)/(raw+cap*per_cost)]
        if j in cluster_indices:limits.append(np.maximum(0,.20*eq-cluster)/(raw+.20*per_cost))
        out=np.maximum(0,np.floor(np.minimum.reduce(limits)))
        return np.where((requested>0)&(raw>anchor),out,0.)
    for k,d in enumerate(p.dates):
        i=int(p.indices[k]);opening=p.opening[k];closing=p.closing[k]
        for day,j,ratio,div,payment in p.actions:
            if day==d:
                qty[:,j]*=ratio;stop[:,j]/=ratio;target[:,j]*=ratio;pending_stop[:,j]/=ratio
                credit=qty[:,j]*div;episode_div[:,j]+=credit
                if credit.any():claims.append((credit,payment))
        pending_claims=[]
        for credit,payment in claims:
            if pd.notna(payment) and pd.Timestamp(payment)<=opening:cash+=credit;paid+=credit
            else:pending_claims.append((credit,payment))
        claims=pending_claims;marks=p.opens[k]
        is_due=(due>=0)&(due<=i)
        for j in sorted_symbols:
            amount=np.where(is_due[:,j],np.maximum(0,qty[:,j]-target[:,j]),0)
            sell(j,amount,marks[j],reason[:,j])
        for j in sorted_symbols:
            request=np.where(is_due[:,j],np.maximum(0,np.floor(target[:,j]-qty[:,j])),0)
            request=np.where(halt|(i<=block)|(marks[j]<=pending_stop[:,j]),0,request)
            q=allowed(j,request,marks[j],pending_stop[:,j],equity(),.10)
            new=(q>0)&(qty[:,j]==0);stop[new,j]=pending_stop[new,j];entry_i[new,j]=i
            px=marks[j]*(1+impact);cash-=q*px;fees+=q*(px-marks[j]);qty[:,j]+=q;basis[:,j]+=q*px;raw_basis[:,j]+=q*marks[j]
            event(j,q,marks[j],px,reason[:,j],np.where(new,'entry','add'))
            due[is_due[:,j],j]=-1
        if benchmark_weight is not None and k==0:
            for j in sorted_symbols:
                raw=marks[j];px=raw*(1+impact);q=np.floor((100000*benchmark_weight/5)/px)
                cash-=q*px;fees+=q*(px-raw);qty[:,j]=q;basis[:,j]=q*px;raw_basis[:,j]=q*raw;entry_i[:,j]=i
                event(j,q,raw,px,7,'entry')
        marks=p.closes[k];pending_claims=[]
        for credit,payment in claims:
            if pd.notna(payment) and pd.Timestamp(payment)<=closing:cash+=credit;paid+=credit
            else:pending_claims.append((credit,payment))
        claims=pending_claims;eq=equity();peak=np.maximum(peak,eq);daily_hit=eq<=previous*(1-.005)
        if benchmark_weight is None:
            halt|=eq<=peak*(1-.03);block=np.where(daily_hit,np.maximum(block,i+1),block)
            forced=np.zeros((n,5),bool)
            for j in range(5):
                forced[:,j]=(qty[:,j]>0)&(halt|daily_hit|(marks[j]<=stop[:,j]))
                r=np.where(halt,3,np.where(daily_hit,4,5));earlier=np.where((due[:,j]>=0)&(target[:,j]<qty[:,j]),due[:,j],i+1+delays)
                f=forced[:,j];due[f,j]=np.minimum(i+1+delays,earlier)[f];target[f,j]=0.;pending_stop[f,j]=stop[f,j];reason[f,j]=r[f]
            used=np.zeros(n);cluster=np.zeros(n)
            for j in sorted_symbols:
                raw=marks[j];value=qty[:,j]*raw;allow=np.minimum.reduce([value,.10*eq,np.maximum(0,.25*eq-used)])
                if j in cluster_indices:allow=np.minimum(allow,np.maximum(0,.20*eq-cluster))
                q=np.where(allow+1e-8<value,np.minimum(qty[:,j],np.floor(allow/raw)),qty[:,j]);used+=q*raw
                if j in cluster_indices:cluster+=q*raw
                reduce=(q<qty[:,j])&~forced[:,j];old=(due[:,j]>=0)&(target[:,j]<qty[:,j]);newtarget=np.minimum(q,np.where(old,target[:,j],q))
                newreason=np.where(old&(newtarget==target[:,j]),reason[:,j],6)
                newdue=np.minimum(i+1+delays,np.where(old,due[:,j],i+1+delays))
                target[reduce,j]=newtarget[reduce];reason[reduce,j]=newreason[reduce];due[reduce,j]=newdue[reduce];pending_stop[reduce,j]=stop[reduce,j]
            if d in p.features:
                eligible=~halt&(i>=block)
                for j in sorted_symbols:
                    f=p.features[d][SYMBOLS[j]]
                    if not f.valid:continue
                    have=qty[:,j];available=eligible&(due[:,j]<0)
                    exit_intent=available&(have>0)&(not f.trend)
                    entry_intent=available&(have==0)&(i>=cooldown[:,j])&f.trend
                    anchor=np.where(have>0,stop[:,j],f.close-3*f.atr)
                    req=np.maximum(0,np.floor(eq*.05/(f.close*(1+impact)))-have)
                    newtarget=have+allowed(j,req,f.close,anchor,eq,.05)
                    chosen=exit_intent|(entry_intent&(anchor>0)&(newtarget!=have))
                    target[chosen,j]=np.where(exit_intent,0.,newtarget)[chosen];pending_stop[chosen,j]=anchor[chosen]
                    due[chosen,j]=(i+1+delays)[chosen];reason[chosen,j]=np.where(exit_intent,2,1)[chosen]
        exposure=np.sum(qty*marks,axis=1);claim=receivable()
        daily[:,k,:]=np.column_stack([eq,cash,eq-100000+fees,eq-100000,fees,claim,paid,exposure,(qty>0).sum(axis=1),daily_hit if benchmark_weight is None else np.zeros(n),halt,(due>=0).sum(axis=1),trades])
        previous=eq.copy()
    if not np.isfinite(daily).all() or (cash < -1e-6).any():raise AssertionError('Nonfinite or negative cash')
    np.testing.assert_allclose(daily[:,:,0],daily[:,:,1]+daily[:,:,5]+daily[:,:,7],atol=1e-6,rtol=0)
    np.testing.assert_allclose(daily[:,:,2]-daily[:,:,4],daily[:,:,3],atol=1e-6,rtol=0)
    path=np.concatenate([np.full((n,1),100000.),daily[:,:,0]],axis=1)
    metrics=pd.DataFrame(dict(final_equity=eq,net_pnl=eq-100000,gross_pnl=eq-100000+fees,modeled_impact=fees,
        max_drawdown=np.max(1-path/np.maximum.accumulate(path,axis=1),axis=1),average_exposure=np.mean(daily[:,:,7]/daily[:,:,0],axis=1),
        trade_count=trades,open_positions=(qty>0).sum(axis=1),pending_orders=(due>=0).sum(axis=1),dividend_receivable=receivable(),
        dividends_paid=paid,drawdown_halt=halt,hypothetical_liquidation_cost=np.sum(qty*marks,axis=1)*impact))
    return dict(daily=daily,fields=FIELDS,fills=pd.concat(events,ignore_index=True) if events else pd.DataFrame(),metrics=metrics,positions=qty.copy(),pending_due=due.copy(),pending_target=target.copy())


def assert_reference_parity(prepared,frames,schedule,actions,cost,delay,benchmark_weight=None):
    from trader_engine.backtest.daily_etf import run_daily_etf_replay
    fast=run_batch(prepared,[cost],[delay],benchmark_weight)
    slow=run_daily_etf_replay(frames,'H1',schedule=schedule,corporate_actions=actions,start=prepared.dates[0],end=prepared.dates[-1],one_way_bps=cost,delay_sessions=delay,benchmark_weight=benchmark_weight)
    actual=fast['daily'][0,:,:-1];expected=slow.equity_curve[list(FIELDS[:-1])].to_numpy(float)
    np.testing.assert_allclose(actual,expected,atol=1e-6,rtol=0)
    fs=slow.decisions.loc[slow.decisions.action.isin(['entry','add','exit','reduce'])].reset_index(drop=True) if not slow.decisions.empty else pd.DataFrame()
    ff=fast['fills'].reset_index(drop=True)
    if len(fs)!=len(ff):raise AssertionError('Fill count differs')
    if len(fs):
        for col in ('timestamp','symbol','action','reason'):
            if list(fs[col])!=list(ff[col]):raise AssertionError('Fill field mismatch: '+col)
        np.testing.assert_allclose(fs[['quantity','price','raw_price']],ff[['quantity','price','raw_price']],atol=1e-8,rtol=0)
        exits=ff.loc[ff.action=='exit']
        assert len(exits)==len(slow.trades)
        if len(exits):
            np.testing.assert_allclose(exits.episode_net,slow.trades.pnl,atol=1e-6,rtol=0)
            np.testing.assert_allclose(exits.episode_gross,slow.trades.gross_pnl,atol=1e-6,rtol=0)
    for field in ('final_equity','net_pnl','gross_pnl','modeled_impact','trade_count','open_positions','dividend_receivable','dividends_paid','drawdown_halt','pending_orders','hypothetical_liquidation_cost'):
        np.testing.assert_allclose(fast['metrics'].iloc[0][field],slow.metrics[field],atol=1e-6,rtol=0,err_msg=field)
    for j,s in enumerate(SYMBOLS):
        np.testing.assert_allclose(fast['positions'][0,j],slow.positions.get(s,{}).get('qty',0),atol=1e-8,rtol=0)
    return dict(cost_bps=cost,delay=delay,benchmark_weight=benchmark_weight,sessions=len(prepared.dates),fills=len(ff),maximum_daily_difference=float(np.abs(actual-expected).max()),passed=True)
