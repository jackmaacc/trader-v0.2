"""Pure modeled benchmark cash/quantity transitions. Never broker or fill evidence."""
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, date, timedelta, time
from decimal import Decimal, ROUND_FLOOR, localcontext
from hashlib import sha256
import json
from zoneinfo import ZoneInfo

from .phase3_benchmarks import BenchmarkTarget, UNIVERSES, EQUITIES, CRYPTO, OPTIONS, SourceStamp, _stamp, _utc
from .phase3_daily import FeeSchedule

D=Decimal


def _n(value, *, positive=False):
    if isinstance(value,(float,bool)):
        raise ValueError('Exact numeric evidence required')
    value=D(value)
    if not value.is_finite() or value<0 or (positive and value==0):
        raise ValueError('Finite nonnegative amount required')
    return value


def _encode(value):
    if is_dataclass(value): return _encode(asdict(value))
    if isinstance(value,(datetime,date)): return value.isoformat()
    if isinstance(value,D): return str(value)
    if isinstance(value,dict): return {k:_encode(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)): return [_encode(v) for v in value]
    return value


def _hash(value):
    return sha256(json.dumps(_encode(value),sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _floor(q,step): return (q/step).to_integral_value(rounding=ROUND_FLOOR)*step


@dataclass(frozen=True)
class SignalPrice:
    price: Decimal
    stamp: SourceStamp


@dataclass(frozen=True)
class ExecutionPrice:
    event_at: datetime
    received_at: datetime
    source: str
    source_sha256: str
    raw_open: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    bid_size: Decimal | None = None
    ask_size: Decimal | None = None


def _schedule(strategy,at):
    at=_utc(at)
    expected=time(0,5) if strategy==CRYPTO else time(9,35) if strategy==OPTIONS else time(9,30)
    local=at if strategy==CRYPTO else at.astimezone(ZoneInfo("America/New_York"))
    if local.time()!=expected:raise ValueError("Frozen execution time required")


def initial_state(strategy_id,control):
    if strategy_id not in UNIVERSES or control not in ('matched','passive'):
        raise ValueError('Frozen strategy and matched/passive control required')
    return {'schema_version':1,'strategy_id':strategy_id,'control':control,'cash':'100000',
            'positions':{},'batches':{},'attempts':{},'modeled_fills':[],
            'passive_initialized':False,'last_applied_at':None,'last_portfolio_at':None,
            'execution_authorized':False,'live_approved':False}


PLAN_FIELDS=('prepare_sha256','target','pre_positions','pre_cash','action_sha256','buys','original_sells','budgets','fees','steps')


def _plan_hash(batch):
    return _hash({key:batch[key] for key in PLAN_FIELDS})


def validate_state(state):
    if state.get('schema_version')!=1 or state.get('strategy_id') not in UNIVERSES or state.get('control') not in ('matched','passive'):
        raise ValueError('Invalid benchmark state')
    if state.get('execution_authorized') is not False or state.get('live_approved') is not False:
        raise ValueError('Offline state cannot authorize execution')
    _n(state['cash'])
    if any(s not in UNIVERSES[state['strategy_id']] for s in state['positions']):
        raise ValueError('Unknown benchmark holding')
    for qty in state['positions'].values(): _n(qty,positive=True)
    if type(state['passive_initialized']) is not bool:
        raise ValueError('Explicit passive initialization state required')
    if state['last_applied_at'] is not None: _utc(datetime.fromisoformat(state['last_applied_at']))
    for batch in state['batches'].values():
        if batch.get('plan_sha256')!=_plan_hash(batch):
            raise ValueError('Frozen benchmark plan integrity mismatch')
        remaining=batch.get('remaining_sells')
        if not isinstance(remaining,dict) or set(remaining)!=set(batch['original_sells']):
            raise ValueError('Remaining sells must retain frozen symbol set')
        for symbol,quantity in remaining.items():
            if _n(quantity)>_n(batch['original_sells'][symbol]):
                raise ValueError('Remaining sells exceed original frozen quantity')
    json.dumps(state,allow_nan=False)


def receivable_value(state):
    """Face-value receivables are equity only; shape defined by phase3_actions."""
    from .phase3_actions import receivable_value as action_receivable_value
    return action_receivable_value(state)


def _fees(strategy,fee):
    if not isinstance(fee,FeeSchedule) or fee.currency not in ('quote','base'):
        raise ValueError('Explicit fee currency required')
    rate=_n(fee.rate);fixed=_n(fee.fixed_cash)
    if rate>=1 or (strategy!=CRYPTO and fee.currency!='quote'):
        raise ValueError('Unsupported benchmark fee model')
    return FeeSchedule(max(rate,D('.0025')) if strategy==CRYPTO else rate,fixed,fee.currency)


def prepare_target(state,batch_id,target,signal_prices,fees,quantity_steps):
    """Freeze deltas and cash ceilings from benchmark-owned equity before execution."""
    with localcontext() as ctx:
        ctx.prec=50
        return _prepare(state,batch_id,target,signal_prices,fees,quantity_steps)


def _prepare(state,batch_id,target,signal_prices,fees,quantity_steps):
    validate_state(state)
    if not isinstance(batch_id,str) or not batch_id or not isinstance(target,BenchmarkTarget):
        raise ValueError('Named target batch required')
    if target.strategy_id!=state['strategy_id'] or target.execution_authorized or target.investment_qualified:
        raise ValueError('Target scope mismatch')
    _schedule(state['strategy_id'],target.timing.execution_at)
    passive=state['control']=='passive'
    if target.control!=('passive_initial' if passive else 'matched'):
        raise ValueError('Target control mismatch')
    universe=set(UNIVERSES[state['strategy_id']])
    if any(set(v)!=universe for v in (target.weights,target.dollars,signal_prices,fees,quantity_steps)):
        raise ValueError('Complete universe evidence required')
    digest=_hash(dict(target=target,signal_prices=signal_prices,fees=fees,steps=quantity_steps))
    if batch_id in state['batches']:
        if state['batches'][batch_id]['prepare_sha256']!=digest: raise ValueError('Conflicting target batch')
        return deepcopy(state)
    if any(b['status']!='complete' for b in state['batches'].values()):
        raise ValueError('Resolve existing target and pending sells before another plan')
    if passive and state['passive_initialized']:
        raise ValueError('Passive holdings cannot rebalance')
    if passive and not target.timing.startup:
        raise ValueError('Passive startup required')
    if state['last_applied_at'] is not None:
        last=_utc(datetime.fromisoformat(state['last_applied_at']))
        if _utc(target.timing.decision_at)<last or _utc(target.timing.execution_at)<=last:
            raise ValueError('Cannot rewind or duplicate benchmark execution boundary')
    prices={};normalized_fees={};steps={}
    for s in sorted(universe):
        if not isinstance(signal_prices[s],SignalPrice):raise ValueError('Timed raw signal price required')
        _stamp(signal_prices[s].stamp,target.timing)
        prices[s]=_n(signal_prices[s].price,positive=True)
        normalized_fees[s]=_fees(state['strategy_id'],fees[s])
        steps[s]=_n(quantity_steps[s],positive=True)
        if state['strategy_id']!=CRYPTO and steps[s]!=1:raise ValueError('Whole-share control required')
    equity=_n(state['cash'])+sum((_n(q)*prices[s] for s,q in state['positions'].items()),D(0))+receivable_value(state)
    if sum((_n(w) for w in target.weights.values()),D(0))>1:
        raise ValueError('No borrowing')
    for s in sorted(universe):
        if abs(_n(target.dollars[s])-_n(target.weights[s])*equity)>D('1e-20'):
            raise ValueError('Target dollars must use benchmark own reconciled equity')
    desired={s:_floor(_n(target.dollars[s])/prices[s],steps[s]) for s in universe}
    sells={s:max(D(0),_n(state['positions'].get(s,'0'))-desired[s]) for s in universe}
    buys={s:_floor(max(D(0),desired[s]-_n(state['positions'].get(s,'0'))),steps[s]) for s in universe}
    # Conservative signal-valued sale proceeds are an upper bound, not spendable cash.
    available=_n(state['cash'])
    for s in sorted(universe):
        q=sells[s];f=normalized_fees[s]
        sale=_floor(q/(1+f.rate if f.currency=='base' else D(1)),steps[s])
        fee=f.fixed_cash+(sale*prices[s]*f.rate if f.currency=='quote' else D(0))
        if sale:available+=max(D(0),sale*prices[s]-fee)
    budgets={}
    for s in sorted(universe):
        budgets[s]=min(available,buys[s]*prices[s]);available-=budgets[s]
    new=deepcopy(state)
    new['batches'][batch_id]=dict(prepare_sha256=digest,target=_encode(target),
        pre_positions=dict(state['positions']),pre_cash=state['cash'],expected_positions=dict(state['positions']),expected_cash=state['cash'],action_sha256=_hash(state.get('corporate_actions',{})),
        buys={s:str(q) for s,q in buys.items()},original_sells={s:str(q) for s,q in sells.items()},remaining_sells={s:str(q) for s,q in sells.items()},
        budgets={s:str(v) for s,v in budgets.items()},fees=_encode(normalized_fees),steps=_encode(steps),
        attempted=False,status='prepared',apply_hash=None,stress=None)
    new['batches'][batch_id]['plan_sha256']=_plan_hash(new['batches'][batch_id])
    if passive:new['passive_initialized']=True
    return new


def _price(observation,strategy,scheduled,now,side,stress):
    if observation is None:return None,'missing_execution_evidence'
    if not isinstance(observation,ExecutionPrice):raise ValueError('Explicit execution evidence required')
    event,received=_utc(observation.event_at),_utc(observation.received_at)
    if not isinstance(observation.source_sha256,str) or len(observation.source_sha256)!=64 or any(c not in '0123456789abcdef' for c in observation.source_sha256):
        raise ValueError('Execution source SHA256 required')
    if event>received or received>now or now<scheduled:return None,'unavailable_or_future_execution_evidence'
    impact=D('.0015') if stress and strategy==CRYPTO else D('.0005') if strategy==CRYPTO else D('.0014') if stress else D('.0007')
    if strategy==EQUITIES:
        if observation.source!='sip' or event!=scheduled:return None,'not_scheduled_raw_open'
        raw=_n(observation.raw_open,positive=True)
    else:
        if observation.source!=('alpaca_crypto' if strategy==CRYPTO else 'sip'):
            return None,'wrong_execution_source'
        if not scheduled<=now<=scheduled+timedelta(seconds=60 if strategy==CRYPTO else 0) or (now-event).total_seconds()>2:
            return None,'stale_or_outside_execution_window'
        bid,ask=_n(observation.bid,positive=True),_n(observation.ask,positive=True)
        if bid>ask:return None,'crossed_quote'
        if strategy==OPTIONS and (observation.bid_size is None or observation.ask_size is None or _n(observation.bid_size)<1 or _n(observation.ask_size)<1 or (ask-bid)/((ask+bid)/2)>D('.10')):
            return None,'invalid_underlying_quote_liquidity'
        raw=ask if side=='buy' else bid
    return raw*(1+impact if side=='buy' else 1-impact),'modeled'


def execute_target(state,batch_id,attempt_id,observations,now,*,stress=False,retry_execution_at=None):
    """First attempt executes frozen sells then buys; retries only retained sells.

    Retry boundary is caller-supplied actual next scheduled session/day. This
    primitive cannot authenticate calendars; no missing buy is silently re-entered.
    """
    with localcontext() as ctx:
        ctx.prec=50
        return _execute(state,batch_id,attempt_id,observations,now,stress,retry_execution_at)


def _execute(state,batch_id,attempt_id,observations,now,stress,retry_execution_at):
    validate_state(state);now=_utc(now)
    if type(stress) is not bool or not isinstance(attempt_id,str) or not attempt_id:
        raise ValueError('Explicit attempt and scenario required')
    if batch_id not in state['batches']:raise ValueError('Prepare target first')
    batch=state['batches'][batch_id]
    digest=_hash(dict(batch_id=batch_id,observations=observations,now=now,stress=stress,retry_execution_at=retry_execution_at))
    if attempt_id in state['attempts']:
        old=state['attempts'][attempt_id]
        if old['sha256']!=digest:raise ValueError('Conflicting execution attempt')
        return deepcopy(state),deepcopy(old['report'])
    if batch['status']=='complete':raise ValueError('Target already completed')
    if state['positions']!=batch['expected_positions'] or state['cash']!=batch['expected_cash']:
        raise ValueError('Prepared cash/ownership changed outside modeled target transition')
    if _hash(state.get('corporate_actions',{}))!=batch['action_sha256']:
        raise ValueError('Corporate action changed prepared target; explicit action-aware replanning required')
    scheduled=_utc(datetime.fromisoformat(batch['target']['timing']['execution_at']))
    decided=_utc(datetime.fromisoformat(batch['target']['timing']['decision_at']))
    if now<decided:raise ValueError('Execution cannot precede decision')
    if batch['attempted']:
        if batch['stress'] is not stress:raise ValueError('Frozen scenario cannot change on retry')
        if retry_execution_at is None:raise ValueError('Explicit next scheduled exit retry required')
        scheduled=_utc(retry_execution_at);_schedule(state['strategy_id'],scheduled)
        if scheduled<=_utc(datetime.fromisoformat(state['last_applied_at'])):raise ValueError('Retry must advance actual execution boundary')
    elif retry_execution_at is not None:raise ValueError('Cannot delay first execution implicitly')
    if state['last_applied_at'] is not None and now<_utc(datetime.fromisoformat(state['last_applied_at'])):raise ValueError('Cannot rewind execution')
    if now<scheduled:raise ValueError('Execution before scheduled boundary')
    universe=UNIVERSES[state['strategy_id']]
    if set(observations)!=set(universe):raise ValueError('Every symbol requires observation or explicit None')
    new=deepcopy(state);b=new['batches'][batch_id];fills=[];issues=[]
    first=not b['attempted']
    for side in ('sell','buy'):
        for s in sorted(universe):
            if side=='buy' and not first:continue
            planned=_n(b['remaining_sells'][s] if side=='sell' else b['buys'][s])
            if planned==0:continue
            px,reason=_price(observations[s],state['strategy_id'],scheduled,now,side,stress)
            if px is None:
                issues.append({'symbol':s,'side':side,'reason':reason});continue
            fee=b['fees'][s];rate=_n(fee['rate']);fixed=_n(fee['fixed_cash']);base=fee['currency']=='base';step=_n(b['steps'][s],positive=True)
            if side=='sell':
                available=min(planned,_n(new['positions'].get(s,'0')))
                qty=_floor(available/(1+rate if base else D(1)),step)
            else:
                budget=min(_n(b['budgets'][s]),_n(new['cash']))
                qty=min(planned,_floor(max(D(0),budget-fixed)/(px*(1+rate if not base else D(1))),step))
            if qty<=0:
                issues.append({'symbol':s,'side':side,'reason':'precision_or_cash_constraint'});continue
            base_fee=qty*rate if base else D(0);quote_fee=fixed+(qty*px*rate if not base else D(0))
            if side=='sell' and qty*px<quote_fee:
                issues.append({'symbol':s,'side':side,'reason':'fees_exceed_proceeds'});continue
            old_qty=_n(new['positions'].get(s,'0'));old_cash=_n(new['cash'])
            if side=='sell':
                units=qty+base_fee;quantity=old_qty-units;cash=old_cash+qty*px-quote_fee
                b['remaining_sells'][s]=str(max(D(0),planned-units))
            else:
                units=qty-base_fee;quantity=old_qty+units;cash=old_cash-qty*px-quote_fee
            if cash<0 or quantity<0:raise ValueError('Modeled ownership/cash invariant failed')
            new['cash']=str(cash)
            if quantity:new['positions'][s]=str(quantity)
            else:new['positions'].pop(s,None)
            fill={'symbol':s,'side':side,'quantity':str(qty),'price':str(px),'base_fee':str(base_fee),'quote_fee':str(quote_fee),
                  'cash_change':str(cash-old_cash),'quantity_change':str(quantity-old_qty),'at':now.isoformat(),'modeled_fill':True}
            fills.append(fill);new['modeled_fills'].append(fill)
    dust={}
    for s in universe:
        remaining=_n(b['remaining_sells'][s]);fee=b['fees'][s]
        divisor=1+_n(fee['rate']) if fee['currency']=='base' else D(1)
        if remaining>0 and _floor(remaining/divisor,_n(b['steps'][s],positive=True))==0:
            dust[s]=str(remaining);b['remaining_sells'][s]='0'
    pending={s:q for s,q in b['remaining_sells'].items() if D(q)>0}
    b['attempted']=True;b['stress']=stress;b['status']='pending_sells' if pending else 'complete'
    b['expected_cash']=new['cash'];b['expected_positions']=dict(new['positions'])
    if not pending:b['apply_hash']=digest
    new['last_applied_at']=now.isoformat();new['last_portfolio_at']=now.isoformat()
    report={'batch_id':batch_id,'attempt_id':attempt_id,'modeled_fills':fills,'issues':issues,'pending_sells':pending,
            'cash':new['cash'],'positions':dict(new['positions']),'dust_owned':dust,'execution_authorized':False,'investment_qualified':False}
    new['attempts'][attempt_id]={'sha256':digest,'report':report}
    validate_state(new)
    return new,deepcopy(report)
