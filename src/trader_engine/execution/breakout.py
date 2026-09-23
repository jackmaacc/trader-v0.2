"""Frozen paper breakout hypothesis and protective holding policy; no auto-tuning."""
from datetime import datetime,timezone,timedelta
from decimal import Decimal,ROUND_FLOOR,ROUND_CEILING
import time

def candidate(bars, now):
    """Last completed minute closes above the prior fifteen-minute high.

    Require contiguous bars, a recent signal, rising close and >=1.2x prior
    volume. Never use the currently forming bar or fill missing bars.
    """
    try:
        completed=[]
        for b in bars:
            stamp=datetime.fromisoformat(b['t'].replace('Z','+00:00'))
            if stamp+timedelta(minutes=1)<=now:
                row={k:Decimal(str(b[k])) for k in ['o','h','l','c','v']}
                if not all(v.is_finite() for v in row.values()):return None
                if not (0<row['l']<=min(row['o'],row['c'])<=max(row['o'],row['c'])<=row['h'] and row['v']>0):return None
                completed.append((stamp,row))
        completed.sort(key=lambda x:x[0]);window=completed[-16:]
        if len(window)!=16 or any(window[i][0]-window[i-1][0]!=timedelta(minutes=1) for i in range(1,16)):return None
        stamp,last=window[-1];past=[r for _,r in window[:-1]]
        if not timedelta(minutes=1)<=now-stamp<=timedelta(minutes=2):return None
        high=max(r['h'] for r in past);volume=sum(r['v'] for r in past)/15
        if not (last['c']>high and last['c']>past[-1]['c'] and last['v']>=volume*Decimal('1.2')):return None
        return {'signal_at':stamp.isoformat(),'breakout_level':str(high),'signal_close':str(last['c']),'relative_volume':str(last['v']/volume)}
    except (ValueError,KeyError,TypeError,ArithmeticError):return None


def hold_breakout(plan,entry,engine,*,get_quote,close_at,should_stop=lambda:False,
                  notify=lambda _:None,sleep=time.sleep,monotonic=time.monotonic,now=lambda:datetime.now(timezone.utc),
                  max_hold_seconds=900, recovery_policy=None, quote_policy=None, quote_feed="iex"):
    """Submit a broker-held stop, then wait for target, stop, time or shutdown.

    Target is monitored locally. Cleanup in PaperExecutor cancels/reconciles the
    protective stop before any market sell of residual shares.
    """
    price=Decimal(entry['filled_avg_price']);qty=Decimal(entry['filled_qty'])
    stop=(price*Decimal('.99')).quantize(Decimal('.01'),rounding=ROUND_FLOOR)
    target=(price*Decimal('1.02')).quantize(Decimal('.01'),rounding=ROUND_CEILING)
    protective=engine._send(plan,plan.exit_ids[0],qty,stop_price=stop)
    # Refused/expired protection must not leave a deliberately held position.
    if protective['status'] in ('rejected','canceled','expired'):raise RuntimeError('Protective stop was not accepted')
    deadline=monotonic()+max_hold_seconds
    info={'status':'holding','symbol':plan.symbol,'quantity':str(qty),'entry_price':str(price),'stop_price':str(stop),'target_price':str(target),'protective_client_id':plan.exit_ids[0],'max_hold_seconds':max_hold_seconds,'started_at':now().isoformat()}
    reason='time_exit'
    while True:
        info['checked_at']=now().isoformat()
        try:notify(info.copy())
        except Exception:engine.stop();reason='reporting_failure';break
        if engine.stop_requested or engine.entries_blocked or engine.storage_failed or should_stop():reason='stop_requested';break
        if now()>=close_at:reason='closing_time';break
        try:
            protective=engine._lookup(plan,plan.exit_ids[0])
        except Exception:
            if recovery_policy is not None:
                recovery_policy.update(plan.symbol,category='order',protection_verified=False,now=monotonic())
            raise
        if recovery_policy is not None and (protective is None or protective.get('status') in ('rejected','canceled','expired')):
            recovery_policy.update(plan.symbol,category='order',protection_verified=False,now=monotonic())
        if protective is None:raise RuntimeError('Protective stop cannot be reconciled')
        if Decimal(protective['filled_qty'])>0:reason='protective_stop';break
        if protective['status'] in ('rejected','canceled','expired'):raise RuntimeError('Protective stop disappeared while holding')
        if monotonic()>=deadline:break
        if recovery_policy is not None:
            from trader_engine.data.quote_validation import QuoteEnvelope,QuotePolicy,validate_quote
            # Confirm exact remaining coverage before accepting a data-only retry.
            verified=(protective.get('status') in ('new','accepted','partially_filled')
                      and Decimal(str(protective.get('qty','0')))==qty
                      and Decimal(str(protective.get('stop_price','0')))==stop)
            if not verified:
                recovery_policy.update(plan.symbol,category='order',protection_verified=False,now=monotonic())
                raise RuntimeError('Protective stop coverage cannot be verified')
            retrieval_error=None
            try:q=get_quote(plan.symbol)
            except Exception as exc:
                if getattr(exc,'status',getattr(exc,'code',None)) in (401,403):
                    recovery_policy.update(plan.symbol,category='account',protection_verified=True,now=monotonic())
                    raise RuntimeError('Account market-data authorization failed') from exc
                q=None;retrieval_error=type(exc).__name__
            at=now().isoformat()
            envelope=q if isinstance(q,QuoteEnvelope) else QuoteEnvelope(plan.symbol,quote_feed,at,at,q)
            # The current decision time must be rechecked even for cached envelopes.
            if isinstance(q,QuoteEnvelope):
                envelope=QuoteEnvelope(q.symbol,q.feed,q.received_at,at,q.raw_payload)
            validation=validate_quote(envelope,quote_policy or QuotePolicy(expected_feed=quote_feed))
            if envelope.symbol != plan.symbol:
                retrieval_error='quote_symbol_mismatch'
            if not validation.valid or retrieval_error:
                diagnostic=validation.to_record()
                if retrieval_error:diagnostic['retrieval_error']=retrieval_error
                info.setdefault('first_quote_failure',diagnostic)
                info['last_quote_failure']=diagnostic
                engine._event({'kind':'holding_quote_rejected',**diagnostic})
                action=recovery_policy.update(plan.symbol,category='data',protection_verified=True,now=monotonic())
                if action.retry_quote:
                    sleep(min(1,max(0,action.deadline-monotonic())))
                    continue
                reason='quote_recovery_expired';break
            action=recovery_policy.update(plan.symbol,category='healthy',protection_verified=True,now=monotonic())
            if action.escalate:
                reason='quote_recovery_expired';break
            q=envelope.raw_payload
        else:
            q=get_quote(plan.symbol)
        try:
            stamp=datetime.fromisoformat(q['t'].replace('Z','+00:00'))
            bid,ask=Decimal(str(q['bp'])),Decimal(str(q['ap']))
            age=(now()-stamp).total_seconds()
            if not (bid.is_finite() and ask.is_finite() and 0<bid<=ask and -.25<=age<=10):raise ValueError('Unusable quote')
            # A tiny future event is not traded on; wait until local time catches up.
            if age<0:
                sleep(min(.3,-age+.01))
                if (now()-stamp).total_seconds()<0:continue
        except (ValueError,KeyError,TypeError,ArithmeticError):raise RuntimeError('Holding quote invalid/stale') from None
        info['last_bid']=str(bid)
        if bid>=target:reason='profit_target';break
        if bid<=stop:reason='stop_level';break
        sleep(min(3,max(0,deadline-monotonic())))
    info.update(status='exiting',exit_reason=reason,ended_at=now().isoformat())
    try:notify(info)
    except Exception:engine.stop()
    engine._event({'kind':'hold_end',**info})
    return reason
