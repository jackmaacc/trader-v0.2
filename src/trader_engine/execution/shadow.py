"""Diagnostic decision capture, never a formal forward run or broker executor."""
from dataclasses import asdict
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import pandas as pd
from trader_engine.agents.team import run_team
from trader_engine.research.strategy_spec import StrategySpec,ETF_UNIVERSE
from trader_engine.research.etf_candidates import mean_reversion_decision,momentum_decision
from trader_engine.data.quote_validation import QuoteEnvelope,QuotePolicy,validate_quote


def run_shadow(*, context, histories, quotes, spec:StrategySpec, risk, positions, cash,
               output_dir, session_open, session_close, previous_session_close=None,
               history_available_at=None):
    """Quotes are QuoteValidationResults; validity is recomputed at context.as_of.

    Input histories are caller-supplied completed bars; MOM additionally requires
    explicit latest-history availability and prior session close. No registration
    may be implied by this diagnostic API. Specialist stances cannot change gates.
    """
    if context.strategy_hash!=spec.spec_hash or set(context.symbols)!=set(ETF_UNIVERSE):
        raise ValueError('Frozen strategy/context identity mismatch')
    if set(histories)!=set(ETF_UNIVERSE):raise ValueError('Complete fixed-universe history required')
    asof=pd.Timestamp(context.as_of);opened=pd.Timestamp(session_open);closed=pd.Timestamp(session_close)
    if opened.tzinfo is None or closed.tzinfo is None or not opened<closed:
        raise ValueError('Explicit timezone-aware exchange session required')
    if not math.isfinite(float(cash)) or cash<0:raise ValueError('Known nonnegative cash required')
    # Deterministic local specialists only; this API accepts no provider or broker.
    team=run_team(context)
    out=Path(output_dir);out.mkdir(parents=True,exist_ok=False)
    decisions=[];owned=[dict(p) for p in positions];remaining=float(cash)
    candidates=[]
    for symbol in ETF_UNIVERSE:
        f=histories[symbol]
        row={'symbol':symbol,'eligible':False,'reason':'unknown','quantity':0,'strategy_hash':spec.spec_hash,
             'as_of':asof.isoformat(),'mode':'diagnostic_shadow','order_authority':False}
        row['history_hash']=sha256(pd.util.hash_pandas_object(f,index=True).values.tobytes()).hexdigest()
        reason=None;signal=None
        if not isinstance(f.index,pd.DatetimeIndex) or f.index.tz is None or not f.index.is_monotonic_increasing or not f.index.is_unique:
            reason='invalid_history_index'
        elif f.empty:reason='insufficient_history'
        elif spec.family=='MR':
            if f.index[-1]+pd.Timedelta(minutes=1)>asof:reason='unfinished_or_future_bar'
            else:
                signal=mean_reversion_decision(f,spec)
                if signal.signal_time is None or not 0<=(asof-pd.Timestamp(signal.signal_time)).total_seconds()<=60:reason='stale_signal'
        else:
            available=(history_available_at or {}).get(symbol)
            previous=pd.Timestamp(previous_session_close) if previous_session_close is not None else None
            if previous is None or previous.tzinfo is None or available is None:reason='daily_availability_missing'
            else:
                available=pd.Timestamp(available)
                if available.tzinfo is None or not previous<=available<=asof or previous>=opened:reason='daily_availability_invalid'
                elif f.index[-1].tz_convert('America/New_York').date()!=previous.tz_convert('America/New_York').date():reason='stale_daily_signal'
                elif f.index[-1]>asof:reason='future_daily_history'
                else:signal=momentum_decision(f,spec)
        if reason is None and signal is not None and not signal.eligible:reason=signal.reason
        if signal is not None:row['signal']=asdict(signal)
        if reason is None and not opened+pd.Timedelta(minutes=30)<=asof<closed-pd.Timedelta(minutes=5):reason='outside_execution_window'
        supplied=quotes.get(symbol)
        if supplied is None:reason=reason or 'missing_quote'
        else:
            envelope=supplied.envelope
            current=QuoteEnvelope(envelope.symbol,envelope.feed,envelope.received_at,asof.isoformat(),envelope.raw_payload)
            validation=validate_quote(current,QuotePolicy(expected_feed='sip'))
            row['quote']=validation.to_record()
            if envelope.symbol!=symbol or not validation.valid:reason=reason or 'invalid_or_stale_quote'
        if reason is not None:
            row['reason']=reason;decisions.append(row);continue
        if symbol in {p['symbol'] for p in owned}:
            row['reason']='already_owned';decisions.append(row);continue
        candidates.append((symbol,signal,row,validation))
    for symbol,signal,row,quote in sorted(candidates,key=lambda x:(-x[1].score,x[0])):
        current_risk=risk.decision()
        if current_risk.allow_entries and (float(current_risk.daily_loss)>=spec.daily_loss_halt or float(current_risk.drawdown)>=spec.drawdown_halt):
            row['reason']='frozen_strategy_risk_halt';decisions.append(row);continue
        if risk.state is not None and risk.state['session']!=str(asof.tz_convert('America/New_York').date()):
            row['reason']='account_session_not_reconciled';decisions.append(row);continue
        if risk.state is None:
            row['reason']='unknown_risk_state';decisions.append(row);continue
        equity=float(risk.state['equity']);impact=spec.one_way_impact;commission=spec.commission_rate
        raw=float(quote.ask);price=raw*(1+impact);stop=price-(1.5 if spec.family=='MR' else 3)*signal.atr
        if stop<=0 or (signal.target is not None and signal.target<=price):
            row['reason']='invalid_stop_or_target';decisions.append(row);continue
        gross=sum(float(p['quantity'])*float(p['price']) for p in owned)
        cluster=sum(float(p['quantity'])*float(p['price']) for p in owned if p['symbol'] in ('SPY','QQQ','IWM'))
        risk_used=sum(float(p['quantity'])*max(0,float(p['price'])-float(p['stop_price'])+float(p.get('exit_cost_per_share',float(p['stop_price'])*(1-(1-impact)*(1-commission))))) for p in owned)
        cap=min(spec.max_gross,spec.max_overnight) if spec.family=='MOM' else spec.max_gross
        per_cost=price*(1+commission)-raw
        stop_cost=price*(1+commission)-stop*(1-impact)*(1-commission)
        limits=[remaining/(price*(1+commission)),spec.max_trade_risk*equity/stop_cost,
                max(0,spec.max_portfolio_risk*equity-risk_used)/(stop_cost+spec.max_portfolio_risk*per_cost),
                max(0,cap*equity-gross)/(raw+cap*per_cost),spec.max_name*equity/(raw+spec.max_name*per_cost)]
        if symbol in ('SPY','QQQ','IWM'):limits.append(max(0,spec.max_equity_cluster*equity-cluster)/(raw+spec.max_equity_cluster*per_cost))
        quantity=max(0,math.floor(min(limits)))
        check=risk.approve_entry(symbol,quantity,price,stop,owned,overnight=spec.family=='MOM',
              one_way_impact_bps=impact*10000,commission_bps=spec.commission_bps,cash=remaining)
        row.update(eligible=check.allow_entries,reason=check.reason,quantity=quantity if check.allow_entries else 0,
                   proposed_entry=price,proposed_stop=stop,risk_decision=asdict(check))
        if check.allow_entries:
            owned.append(dict(symbol=symbol,quantity=quantity,price=price,stop_price=stop,overnight=spec.family=='MOM'))
            remaining-=quantity*price*(1+commission)
        decisions.append(row)
    def write(name,value):
        with (out/name).open('x') as f:
            json.dump(value,f,sort_keys=True,default=str,allow_nan=False);f.write('\n');f.flush();os.fsync(f.fileno())
    write('advisory.json',asdict(team))
    write('decisions.json',decisions)
    write('manifest.json',{'mode':'diagnostic_shadow','formal_forward_run':False,'order_authority':False,
          'strategy':asdict(spec),'strategy_hash':spec.spec_hash,'context_hash':context.input_hash,
          'as_of':asof.isoformat(),'account_risk':asdict(risk.decision())})
    return {'mode':'diagnostic_shadow','formal_forward_run':False,'team':team,'decisions':decisions,'output_dir':str(out)}
