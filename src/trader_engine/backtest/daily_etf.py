"""Daily-session adapter for the shared ETF research engine.

Uses the same marked-equity, receivable, cost and result conventions as minute replay.
No network or broker access. Daily closes request execution at later actual raw opens.
"""
from hashlib import sha256
import json
from math import floor, isfinite
import numpy as np
import pandas as pd
from trader_engine.research.strategy_spec import ETF_UNIVERSE
from trader_engine.research.daily_hypotheses import (
    DAILY_CANDIDATES, HoldingState, daily_feature, decision_due, target_intents,
)


def run_daily_etf_replay(frames_by_symbol, candidate_id, initial_capital=100000., *,
                         schedule, corporate_actions=None, start=None, end=None,
                         one_way_bps=7., delay_sessions=0, benchmark_weight=None):
    from trader_engine.backtest.engine import ETFReplayResult, _etf_mark_to_stop_risk
    if candidate_id not in DAILY_CANDIDATES:
        raise ValueError('Unknown frozen daily candidate')
    if not isfinite(initial_capital) or initial_capital <= 0:
        raise ValueError('Positive finite capital required')
    if not isfinite(one_way_bps) or not 0 <= one_way_bps < 10000:
        raise ValueError('Invalid one-way execution drag')
    if type(delay_sessions) is not int or delay_sessions not in (0, 1):
        raise ValueError('Daily delay must be zero or one additional session')
    if benchmark_weight not in (None, .25, 1.):
        raise ValueError('Only frozen 25% and 100% passive baskets supported')
    if set(frames_by_symbol) != set(ETF_UNIVERSE):
        raise ValueError('Complete fixed five-ETF universe required')
    cal = schedule.copy()
    for col in ('market_open', 'market_close'):
        cal[col] = pd.to_datetime(cal[col], utc=True)
    if cal.empty or not cal.market_open.is_monotonic_increasing or cal.market_open.duplicated().any():
        raise ValueError('Ordered unique actual-session calendar required')
    if (cal.market_close <= cal.market_open).any() or any(cal.market_open.iloc[i] <= cal.market_close.iloc[i-1] for i in range(1,len(cal))):
        raise ValueError('Invalid session bounds')
    dates = pd.DatetimeIndex([x.date() for x in cal.market_open.dt.tz_convert('America/New_York')])
    if dates.has_duplicates:
        raise ValueError('Duplicate exchange session')
    cal.index = dates
    lo = dates[0] if start is None else pd.Timestamp(start).normalize()
    hi = dates[-1] if end is None else pd.Timestamp(end).normalize()
    active = dates[(dates >= lo) & (dates <= hi)]
    if active.empty: raise ValueError('No evaluation sessions')
    frames = {}
    for s, f in frames_by_symbol.items():
        if not isinstance(f.index, pd.DatetimeIndex) or f.index.has_duplicates or not f.index.is_monotonic_increasing:
            raise ValueError('Ordered unique daily session labels required')
        f = f.copy(); f.index = pd.DatetimeIndex([pd.Timestamp(t).date() for t in f.index])
        if f.index.has_duplicates: raise ValueError('Duplicate daily session labels')
        cols = ['open', 'high', 'low', 'close', 'total_return_close']
        if not set(cols).issubset(f): raise ValueError('Raw OHLC and total-return close required')
        a = f[cols].to_numpy(float)
        if not np.isfinite(a).all() or (a <= 0).any(): raise ValueError('Invalid supplied daily observation')
        if ((f.high < f[['open', 'close']].max(axis=1)) | (f.low > f[['open', 'close']].min(axis=1))).any():
            raise ValueError('Inconsistent raw OHLC')
        frames[s] = f
    actions = pd.DataFrame() if corporate_actions is None else corporate_actions.copy()
    if not actions.empty:
        if actions.index.tz is None or not actions.index.is_monotonic_increasing:
            raise ValueError('Ordered timezone-aware corporate actions required')
        if not set(actions.symbol).issubset(ETF_UNIVERSE): raise ValueError('Unknown action symbol')
        action_keys = [(t.tz_convert('America/New_York').date(), a.symbol) for t,a in actions.iterrows()]
        if len(action_keys) != len(set(action_keys)): raise ValueError('Ambiguous duplicate symbol/ex-date corporate events')
        for t, a in actions.iterrows():
            ratio, div = float(a.get('split_ratio', 1)), float(a.get('cash_dividend', 0))
            if not isfinite(ratio) or ratio <= 0 or not isfinite(div) or div < 0:
                raise ValueError('Invalid corporate action')
            pay = a.get('payment_timestamp', pd.NaT)
            if pd.notna(pay) and (pd.Timestamp(pay).tzinfo is None or pd.Timestamp(pay) < t):
                raise ValueError('Invalid distribution payment time')
    impact = one_way_bps/10000
    config = dict(candidate_id=candidate_id, one_way_bps=one_way_bps,
                  delay_sessions=delay_sessions, benchmark_weight=benchmark_weight,
                  stop_anchor='signal_close_minus_3_atr20', version='daily-etf-v1')
    spec_hash = sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    cash = float(initial_capital); positions = {}; marks = {}; claims = []; pending = {}
    decisions = []; trades = []; ledger = []; fees = 0.; paid = 0.; peak = initial_capital
    previous_close = initial_capital; halt = False; daily_block_until = -1; cooldown = {}
    realized = {s: 0. for s in ETF_UNIVERSE}; gross_realized = realized.copy()
    data_complete = True; valuation_valid = True; benchmark_initialized = False
    action_cursor = 0
    # Earlier actions affect signal scales, but no holding existed before the start.
    while action_cursor < len(actions) and actions.index[action_cursor] < cal.loc[active[0], 'market_open']:
        if actions.index[action_cursor].tz_convert('America/New_York').date() >= active[0].date(): break
        action_cursor += 1

    def equity():
        return cash + sum(p['qty']*marks[s] for s, p in positions.items()) + sum(c['amount'] for c in claims)

    def log(t, s, reason, action='reject', **more):
        decisions.append(dict(timestamp=t, symbol=s, reason=reason, action=action, spec_hash=spec_hash, **more))

    def sell(s, qty, raw, t, reason, i):
        nonlocal cash, fees
        p = positions[s]; qty = min(qty, p['qty']); fraction = qty/p['qty']
        px = raw*(1-impact); cost = qty*(raw-px)
        basis, raw_basis = p['basis']*fraction, p['raw_basis']*fraction
        cash += qty*px; fees += cost
        pnl, gross = qty*px-basis, qty*raw-raw_basis
        realized[s] += pnl; gross_realized[s] += gross
        p['episode_net'] += pnl; p['episode_gross'] += gross
        p['basis'] -= basis; p['raw_basis'] -= raw_basis; p['qty'] -= qty
        log(t, s, reason, 'exit' if p['qty'] < 1e-9 else 'reduce', price=px, raw_price=raw, quantity=qty)
        if p['qty'] < 1e-9:
            trades.append(dict(symbol=s, entry_time=p['entry_time'], exit_time=t,
                quantity=p['episode_quantity'], pnl=p['episode_net']+p['dividends'],
                gross_pnl=p['episode_gross']+p['dividends'], reason=reason,
                completed_sessions=i-p['entry_i'], entry_session=p['entry_session'], exit_session=dates[i].date()))
            del positions[s]
            if benchmark_weight is None and (candidate_id == 'H3' or reason == 'protective_exit'):
                cooldown[s] = i+10

    def allowed_add(s, requested, raw, stop, eq, cap_weight):
        if requested <= 0 or raw <= stop: return 0
        gross = sum(p['qty']*marks[k] for k,p in positions.items())
        cluster = sum(p['qty']*marks[k] for k,p in positions.items() if k in ('SPY','QQQ','IWM'))
        risk = sum(_etf_mark_to_stop_risk(p['qty'], marks[k], p['stop'], impact, 0.) for k,p in positions.items())
        existing = positions.get(s, {}).get('qty', 0.)
        px = raw*(1+impact); per_cost = px-raw; stop_cost = px-stop*(1-impact)
        existing_risk = _etf_mark_to_stop_risk(existing,raw,stop,impact,0.)
        limits = [requested, cash/px,
            max(0, .001*eq-existing_risk)/(stop_cost+.001*per_cost),
            max(0, .005*eq-risk)/(stop_cost+.005*per_cost),
            max(0, .25*eq-gross)/(raw+.25*per_cost),
            max(0, cap_weight*eq-existing*raw)/(raw+cap_weight*per_cost)]
        if s in ('SPY','QQQ','IWM'):
            limits.append(max(0, .20*eq-cluster)/(raw+.20*per_cost))
        return max(0, floor(min(limits)))

    def signal_frame(s, d):
        f = frames[s].loc[:d].copy()
        if not actions.empty:
            # Adjust earlier bars only for splits economically effective by this close.
            for t,a in actions[(actions.symbol == s) & (actions.index <= cal.loc[d,'market_close'])].iterrows():
                ratio = float(a.get('split_ratio',1.))
                if ratio != 1:
                    before = f.index < pd.Timestamp(t.tz_convert('America/New_York').date())
                    f.loc[before,['open','high','low','close']] /= ratio
        f['signal_scale'] = 1.
        return f

    for d in active:
        i = dates.get_loc(d); opening, closing = cal.loc[d, ['market_open','market_close']]
        rows = {s:f.loc[d] for s,f in frames.items() if d in f.index}
        if len(rows) != len(ETF_UNIVERSE): data_complete = False
        # Entitlements are applied before open transactions, to pre-open owners.
        while action_cursor < len(actions) and actions.index[action_cursor] <= opening:
            t = actions.index[action_cursor]; a = actions.iloc[action_cursor]; s = a.symbol
            ratio, div = float(a.get('split_ratio',1.)), float(a.get('cash_dividend',0.))
            if s in positions:
                p=positions[s]; p['qty'] *= ratio; p['stop'] /= ratio
                if s in marks: marks[s] /= ratio
                credit = p['qty']*div; p['dividends'] += credit
                realized[s] += credit; gross_realized[s] += credit
                if credit: claims.append(dict(symbol=s, amount=credit, payment_timestamp=a.get('payment_timestamp',pd.NaT)))
            if s in pending:
                pending[s]['target_qty'] *= ratio; pending[s]['stop'] /= ratio
            action_cursor += 1
        # Daily adapter needs ex-date events no later than the session open.
        if action_cursor < len(actions) and actions.index[action_cursor] <= closing:
            raise ValueError('Daily corporate entitlement must be effective by session open')
        unpaid=[]
        for claim in claims:
            pay=claim['payment_timestamp']
            if pd.notna(pay) and pd.Timestamp(pay) <= opening: cash += claim['amount']; paid += claim['amount']
            else: unpaid.append(claim)
        claims=unpaid
        for s,r in rows.items(): marks[s]=float(r.open)
        stale = [s for s in positions if s not in rows]
        if stale: valuation_valid=False
        due = {s:o for s,o in pending.items() if o['due'] <= i}
        for s,o in sorted(due.items()):
            if s not in rows:
                have=positions.get(s,{}).get('qty',0.)
                log(opening,s,'missing_exit_open_pending' if o['target_qty'] < have else 'missing_entry_open_expired')
                if o['target_qty'] >= have: pending.pop(s,None)
                continue
            have=positions.get(s,{}).get('qty',0.)
            if o['target_qty'] < have:
                sell(s,have-o['target_qty'],float(rows[s].open),opening,o['reason'],i)
        for s,o in sorted(due.items()):
            if s not in rows: continue
            raw=float(rows[s].open); have=positions.get(s,{}).get('qty',0.)
            request=max(0,floor(o['target_qty']-have))
            if request:
                if halt or i <= daily_block_until or stale:
                    log(opening,s,'halt_or_stale_mark'); pending.pop(s,None); continue
                if raw <= o['stop']:
                    log(opening,s,'entry_open_breaches_signal_stop'); pending.pop(s,None); continue
                qty=allowed_add(s,request,raw,o['stop'],equity(),.10)
                if qty:
                    px=raw*(1+impact); cash-=qty*px; fees+=qty*(px-raw)
                    if s not in positions:
                        positions[s]=dict(qty=0.,basis=0.,raw_basis=0.,stop=o['stop'],entry_time=opening,
                            entry_session=d.date(),entry_i=i,episode_net=0.,episode_gross=0.,dividends=0.,episode_quantity=0.)
                    p=positions[s];p['qty']+=qty;p['basis']+=qty*px;p['raw_basis']+=qty*raw;p['episode_quantity']+=qty
                    log(opening,s,o['reason'],'entry' if have == 0 else 'add',price=px,raw_price=raw,quantity=qty)
                else: log(opening,s,'risk_capacity')
            pending.pop(s,None)
        if benchmark_weight is not None and not benchmark_initialized:
            benchmark_initialized=True
            # All five opens required: no retrospective shift to a favorable later day.
            if len(rows)!=5: raise ValueError('Passive basket initial raw open missing')
            allocation=initial_capital*benchmark_weight/5
            for s in sorted(ETF_UNIVERSE):
                raw=float(rows[s].open);qty=floor(allocation/(raw*(1+impact)));px=raw*(1+impact)
                if qty:
                    cash-=qty*px;fees+=qty*(px-raw)
                    positions[s]=dict(qty=float(qty),basis=qty*px,raw_basis=qty*raw,stop=0.,entry_time=opening,
                        entry_session=d.date(),entry_i=i,episode_net=0.,episode_gross=0.,dividends=0.,episode_quantity=qty)
                    log(opening,s,'passive_initialization','entry',price=px,raw_price=raw,quantity=qty)
        for s,r in rows.items(): marks[s]=float(r.close)
        # Payments during the session are available for the next open, never retroactively.
        unpaid=[]
        for claim in claims:
            pay=claim['payment_timestamp']
            if pd.notna(pay) and pd.Timestamp(pay)<=closing:cash+=claim['amount'];paid+=claim['amount']
            else:unpaid.append(claim)
        claims=unpaid
        eq=equity();peak=max(peak,eq)
        daily_hit=eq <= previous_close*(1-.005)
        if benchmark_weight is None:
            if eq <= peak*(1-.03): halt=True
            if daily_hit: daily_block_until=max(daily_block_until,i+1)
            forced={}
            for s,p in positions.items():
                if halt: forced[s]='drawdown_halt'
                elif daily_hit: forced[s]='daily_loss_halt'
                elif s in rows and float(rows[s].close)<=p['stop']:forced[s]='protective_exit'
            for s,reason in forced.items():
                old=pending.get(s,{})
                earlier=old.get('due',i+1+delay_sessions) if old.get('target_qty',positions[s]['qty']) < positions[s]['qty'] else i+1+delay_sessions
                due=min(i+1+delay_sessions,earlier)
                pending[s]=dict(target_qty=0.,stop=positions[s]['stop'],due=due,reason=reason)
            # Enforce marked exposure caps at the next open, including price drift.
            used=0.;cluster=0.
            for s,p in sorted(positions.items()):
                raw=marks[s];value=p['qty']*raw
                allowed=min(value,.10*eq,max(0,.25*eq-used))
                if s in ('SPY','QQQ','IWM'): allowed=min(allowed,max(0,.20*eq-cluster))
                qty=min(p['qty'],floor(allowed/raw)) if allowed+1e-8<value else p['qty']
                used+=qty*raw
                if s in ('SPY','QQQ','IWM'):cluster+=qty*raw
                if qty < p['qty'] and s not in forced:
                    old=pending.get(s,{})
                    if old.get('target_qty',p['qty']) >= p['qty']: old={}
                    target=min(qty,old.get('target_qty',qty))
                    due=min(i+1+delay_sessions,old.get('due',i+1+delay_sessions))
                    pending[s]=dict(target_qty=target,stop=p['stop'],due=due,reason=old.get('reason','exposure_cap_reduction') if target == old.get('target_qty') else 'exposure_cap_reduction')
            if i+1 < len(dates) and not halt and i >= daily_block_until and decision_due(candidate_id,d,dates[i+1]):
                features={s:daily_feature(candidate_id,signal_frame(s,d),dates,d) for s in ETF_UNIVERSE}
                states={s:HoldingState(i-p['entry_i']+1,p['qty']*marks[s]/eq) for s,p in positions.items()}
                blocked={s for s,until in cooldown.items() if i < until} | set(pending)
                intents=target_intents(candidate_id,features,states,blocked_symbols=blocked)
                for s,f in features.items():
                    if not f.valid: log(closing,s,f.reason)
                for s,intent in sorted(intents.items()):
                    if s in pending: continue
                    have=positions.get(s,{}).get('qty',0.)
                    if intent.target_weight==0:
                        target=0.;stop=positions[s]['stop']
                    else:
                        f=features[s]
                        if not f.valid:continue
                        stop=positions[s]['stop'] if have else f.close-3*f.atr
                        if stop <= 0:log(closing,s,'invalid_signal_stop');continue
                        target=floor(eq*intent.target_weight/(f.close*(1+impact)))
                        if target > have:
                            target=have+allowed_add(s,target-have,f.close,stop,eq,min(.10,intent.target_weight))
                    if target != have:
                        pending[s]=dict(target_qty=target,stop=stop,due=i+1+delay_sessions,reason=intent.reason)
                        log(closing,s,intent.reason,'plan',target_quantity=target,stop=stop,due_session_number=i+1+delay_sessions)
        exposure=sum(p['qty']*marks[s] for s,p in positions.items())
        gross=eq-initial_capital+fees
        ledger.append(dict(timestamp=closing,session=d.date(),equity=eq,broker_equity=eq,cash=cash,
            cumulative_net_pnl=eq-initial_capital,cumulative_gross_pnl=gross,cumulative_fees=0.,
            cumulative_impact_cost=fees,dividend_receivable=sum(c['amount'] for c in claims),dividends_paid=paid,
            gross_exposure=exposure,open_positions=len(positions),daily_halt=daily_hit if benchmark_weight is None else False,
            drawdown_halt=halt,stale_symbols=tuple(stale),pending_orders=len(pending),overhead=0.))
        previous_close=eq
    contributions=[]
    for s in ETF_UNIVERSE:
        p=positions.get(s); net=realized[s];gross=gross_realized[s]
        if p:net+=p['qty']*marks[s]-p['basis'];gross+=p['qty']*marks[s]-p['raw_basis']
        contributions.append(dict(symbol=s,net_pnl=net,gross_pnl=gross))
    net=equity()-initial_capital;gross=net+fees
    if abs(sum(c['net_pnl'] for c in contributions)-net)>1e-6:raise AssertionError('Daily net accounting identity failed')
    if abs(sum(c['gross_pnl'] for c in contributions)-gross)>1e-6:raise AssertionError('Daily gross accounting identity failed')
    liquidation_cost=sum(p['qty']*marks[s]*impact for s,p in positions.items())
    metrics=dict(config, benchmark_delay_applied=False, spec_hash=spec_hash, gross_pnl=gross,net_pnl=net,final_equity=equity(),
        hypothetical_liquidation_equity=equity()-liquidation_cost, hypothetical_liquidation_cost=liquidation_cost,
        modeled_impact=fees,fees=0.,sessions=len(active),trade_count=len(trades),open_positions=len(positions),
        dividend_receivable=sum(c['amount'] for c in claims),dividends_paid=paid,drawdown_halt=halt,
        data_complete=data_complete,valuation_valid=valuation_valid,overhead_known=False,business_pnl=None,
        pending_orders=len(pending),overhead=0.)
    return ETFReplayResult(pd.DataFrame(ledger),pd.DataFrame(trades),pd.DataFrame(decisions),positions,metrics,pd.DataFrame(contributions))
