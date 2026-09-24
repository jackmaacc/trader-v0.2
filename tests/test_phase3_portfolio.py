from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
import json
import pytest
from trader_engine.research.phase3_daily import CRYPTO, EQUITIES, UNIVERSES, FeeSchedule, SignalDecision, PriceObservation
from trader_engine.research.phase3_portfolio import Mark, initial_state, set_day_baseline, prepare_batch, apply_batch, validate_state

DAY = date(2026, 10, 1)
AT = datetime(2026, 10, 1, 13, 30, tzinfo=timezone.utc)


def state(strategy=EQUITIES, baseline=True):
    value = initial_state(strategy, DAY)
    if baseline:
        value = set_day_baseline(value, DAY, D(100000), boundary_at=datetime(2026,10,1,tzinfo=timezone.utc), provenance='fixture-reconciled-no-cashflows')
    return value


def inputs(s, at=AT, actions=None, price=D(100), currency='quote'):
    strategy=s['strategy_id']; universe=UNIVERSES[strategy]
    decisions={}
    for symbol in universe:
        held=D(s['positions'].get(symbol,'0'))
        action=(actions or {}).get(symbol,'hold' if held else 'enter')
        decisions[symbol]=SignalDecision(strategy,symbol,action,'fixture',at.date()-timedelta(days=1),at,price,held,action=='exit' or symbol in s['pending_exits'],at)
    marks={symbol:Mark(price,at,at,'sip' if strategy==EQUITIES else 'alpaca_crypto') for symbol in universe}
    fees={symbol:FeeSchedule(D(0),currency=currency) for symbol in universe}
    steps={symbol:D(1) if strategy==EQUITIES else D('.001') for symbol in universe}
    observations={symbol:PriceObservation(at,at,raw_open=price,bid=price,ask=price) for symbol in universe}
    return decisions,marks,fees,steps,observations


def prepared(s, batch='b1', **kwargs):
    ds,marks,fees,steps,observations=inputs(s,**kwargs)
    at=kwargs.get('at',AT)
    return prepare_batch(s,batch,ds,marks,at,fees,steps),marks,observations


def test_flat_start_bounded_entries_and_json_roundtrip():
    s=state(); original=deepcopy(s); pending,marks,observations=prepared(s)
    result,report=apply_batch(json.loads(json.dumps(pending)),'b1',observations,marks,AT)
    assert s==original
    assert [f['symbol'] for f in report['modeled_fills']]==['IWM','QQQ','SPY']
    assert all(f['modeled_fill'] and not f['execution_authorized'] for f in report['modeled_fills'])
    assert all(D(q)<=80 for q in result['positions'].values())
    assert D(report['gross'])<=D(report['equity'])*D('.24')
    assert D(report['cash'])+D(report['gross'])==D(report['equity'])
    validate_state(json.loads(json.dumps(result)))


def test_idempotent_hash_replay_and_conflicts():
    pending,marks,observations=prepared(state())
    result,report=apply_batch(pending,'b1',observations,marks,AT)
    replay,again=apply_batch(result,'b1',observations,marks,AT)
    assert replay==result and again['replayed'] and len(replay['modeled_fills'])==3
    changed=dict(observations);changed['SPY']=None
    with pytest.raises(ValueError,match='conflict'):apply_batch(result,'b1',changed,marks,AT)
    ds,m,fees,steps,_=inputs(state()); ds['SPY']=deepcopy(ds['IWM'])
    with pytest.raises(ValueError):prepare_batch(pending,'b1',ds,m,AT,fees,steps)


def test_missing_baseline_blocks_without_inventing_prior_close():
    pending,marks,observations=prepared(state(baseline=False))
    result,report=apply_batch(pending,'b1',observations,marks,AT)
    assert result['positions']=={} and not report['modeled_fills']
    assert result['prior_utc_close_equity'] is None


def test_maximum_original_plan_cannot_grow_on_lower_execution_price():
    pending,marks,observations=prepared(state())
    observations={s:PriceObservation(AT,AT,raw_open=D(50)) for s in observations}
    result,report=apply_batch(pending,'b1',observations,marks,AT)
    assert all(D(f['quantity'])<=D(pending['batches']['b1']['plans'][f['symbol']]['quantity']) for f in report['modeled_fills'])
    changed=deepcopy(pending);changed['batches']['b1']['plans']['SPY']['quantity']='9999'
    with pytest.raises(ValueError,match='content changed'):apply_batch(changed,'b1',observations,marks,AT)


def test_missing_exit_preserved_and_entry_processed_after_exit():
    s=state();s['cash']='90000';s['positions']={'SPY':'100'}
    actions={'SPY':'exit','IWM':'enter','QQQ':'enter'}
    pending,marks,observations=prepared(s,actions=actions);observations['SPY']=None
    result,report=apply_batch(pending,'b1',observations,marks,AT)
    assert result['positions']['SPY']=='100' and result['pending_exits']==['SPY']
    assert report['blocked']['SPY']=='missing_execution_observation'
    pending,marks,observations=prepared(s,actions=actions)
    result,report=apply_batch(pending,'b1',observations,marks,AT)
    assert report['modeled_fills'][0]['side']=='sell'
    assert 'SPY' not in result['positions']
    reentry,m,o=prepared(result,batch='b2',actions={'SPY':'enter','IWM':'hold','QQQ':'hold'})
    final,again=apply_batch(reentry,'b2',o,m,AT)
    assert 'SPY' not in final['positions'] and again['blocked']['SPY']=='session_entry_already_attempted_or_closed'


def test_missing_entry_not_retried_via_new_batch_id():
    pending,marks,observations=prepared(state());observations['SPY']=None
    result,_=apply_batch(pending,'b1',observations,marks,AT)
    reentry,m,o=prepared(result,batch='b2',actions={'SPY':'enter','IWM':'hold','QQQ':'hold'})
    final,report=apply_batch(reentry,'b2',o,m,AT)
    assert 'SPY' not in final['positions'] and 'SPY' in report['blocked']


def test_base_asset_fees_and_dust_retained():
    at=datetime(2026,10,1,0,5,tzinfo=timezone.utc)
    pending,marks,observations=prepared(state(CRYPTO),at=at,currency='base')
    bought,report=apply_batch(pending,'b1',observations,marks,at)
    for fill in report['modeled_fills']:
        assert D(fill['acquired_quantity'])==D(fill['quantity'])-D(fill['base_fee'])
        assert D(bought['positions'][fill['symbol']])==D(fill['acquired_quantity'])
    next_at=at+timedelta(days=1)
    bought=set_day_baseline(bought,next_at.date(),D(report['equity']),boundary_at=datetime(2026,10,2,tzinfo=timezone.utc),provenance='fixture-day2')
    pending,marks,observations=prepared(bought,batch='exit',at=next_at,currency='base',actions={s:'exit' for s in UNIVERSES[CRYPTO]})
    sold,report=apply_batch(pending,'exit',observations,marks,next_at)
    for fill in report['modeled_fills']:
        s=fill['symbol'];assert D(bought['positions'][s])-D(fill['owned_units_debit'])==D(sold['positions'].get(s,'0'))
    assert sold['pending_exits'] # precision dust must not disappear
    assert all(D(q)>0 for q in sold['positions'].values())


@pytest.mark.parametrize('failure',['omit_decision','ownership'])
def test_missing_or_noncausal_evidence_fails_atomically(failure):
    s=state();ds,m,fees,steps,o=inputs(s);original=deepcopy(s)
    if failure=='omit_mark':del m['SPY']
    elif failure=='future_mark':m['SPY']=Mark(D(100),AT,AT+timedelta(seconds=1),'sip')
    elif failure=='stale_mark':m['SPY']=Mark(D(100),AT-timedelta(seconds=3),AT,'sip')
    elif failure=='omit_decision':del ds['SPY']
    else:s['positions']['SPY']='1';original=deepcopy(s)
    with pytest.raises(ValueError):prepare_batch(s,'b',ds,m,AT,fees,steps)
    assert s==original


def test_latched_loss_and_day_provenance():
    s=state();s['cash']='98000'
    pending,marks,observations=prepared(s)
    assert pending['entry_halted']
    pending['cash']='100500' # synthetic later recovery; latch remains
    result,report=apply_batch(pending,'b1',observations,marks,AT)
    assert result['entry_halted'] and not report['modeled_fills']
    with pytest.raises(ValueError,match='cannot be replaced'):
        set_day_baseline(result,DAY,D(100500),boundary_at=datetime(2026,10,1,tzinfo=timezone.utc),provenance='replacement')
    with pytest.raises(ValueError,match='exact UTC'):
        set_day_baseline(result,DAY+timedelta(days=1),D(100500),boundary_at=AT,provenance='bad')
    next_day=set_day_baseline(result,DAY+timedelta(days=1),D(100500),boundary_at=datetime(2026,10,2,tzinfo=timezone.utc),provenance='verified-day2')
    assert next_day['entry_halted'] is False


@pytest.mark.parametrize('failure',['omit_mark','future_mark','stale_mark'])
def test_bad_marks_block_entries_without_inventing_valuation(failure):
    s=state(); ds,m,fees,steps,o=inputs(s)
    if failure=='omit_mark':del m['SPY']
    elif failure=='future_mark':m['SPY']=Mark(D(100),AT,AT+timedelta(seconds=1),'sip')
    else:m['SPY']=Mark(D(100),AT-timedelta(seconds=3),AT,'sip')
    pending=prepare_batch(s,'b',ds,m,AT,fees,steps)
    result,report=apply_batch(pending,'b',o,m,AT)
    assert 'SPY' not in result['positions']
    if failure!='omit_mark':assert report['equity'] is None


def test_missing_unrelated_mark_does_not_block_owned_exit():
    at=datetime(2026,10,1,0,5,tzinfo=timezone.utc)
    s=state(CRYPTO);s['cash']='80000';s['positions']={'BTC/USD':'100','ETH/USD':'100'}
    ds,m,fees,steps,o=inputs(s,at=at,actions={'BTC/USD':'exit','ETH/USD':'hold'})
    del m['ETH/USD']
    pending=prepare_batch(s,'exit',ds,m,at,fees,steps)
    result,report=apply_batch(pending,'exit',o,m,at)
    assert 'BTC/USD' not in result['positions'] and result['positions']['ETH/USD']=='100'
    assert report['equity'] is None and report['gross'] is None and not report['valuation_available']


def test_unresolved_prepared_batch_blocks_overlapping_reservations():
    pending,m,o=prepared(state())
    ds,m,fees,steps,o=inputs(state())
    with pytest.raises(ValueError,match='one unresolved'):
        prepare_batch(pending,'another',ds,m,AT,fees,steps)


def test_prepare_cannot_freeze_past_state_after_later_execution():
    pending,m,o=prepared(state())
    result,_=apply_batch(pending,'b1',o,m,AT)
    old=AT-timedelta(seconds=1)
    ds,m,fees,steps,o=inputs(result,at=old)
    with pytest.raises(ValueError,match='preparation clock regression'):
        prepare_batch(result,'past',ds,m,old,fees,steps)


def test_baseline_cannot_rewind_after_next_day_exit_without_baseline():
    s=state();s['cash']='90000';s['positions']={'SPY':'100'}
    tomorrow=AT+timedelta(days=1)
    pending,m,o=prepared(s,at=tomorrow,actions={'SPY':'exit','IWM':'flat','QQQ':'flat'})
    result,report=apply_batch(pending,'b1',o,m,tomorrow)
    assert 'SPY' not in result['positions'] and not report['valuation_available']
    assert result['utc_day']==DAY.isoformat()
    with pytest.raises(ValueError,match='predates applied execution'):
        set_day_baseline(result,DAY,D(100000),boundary_at=datetime(2026,10,1,tzinfo=timezone.utc),provenance='fixture-reconciled-no-cashflows')


def test_preparation_timestamp_prevents_execution_time_travel():
    s=state();ds,m,fees,steps,o=inputs(s)
    late=AT+timedelta(hours=2)
    late_marks={symbol:Mark(mark.price,late,late,mark.source) for symbol,mark in m.items()}
    pending=prepare_batch(s,'future-preparation',ds,late_marks,late,fees,steps)
    assert pending['batches']['future-preparation']['prepared_at']==late.isoformat()
    with pytest.raises(ValueError,match='cannot precede batch preparation'):
        apply_batch(pending,'future-preparation',o,m,AT)
    tampered=deepcopy(pending);tampered['batches']['future-preparation']['prepared_at']=AT.isoformat()
    with pytest.raises(ValueError,match='content changed'):
        apply_batch(tampered,'future-preparation',o,m,AT)


def test_late_preparation_cannot_freeze_new_entry_after_open():
    s=state();ds,m,fees,steps,o=inputs(s)
    late=AT+timedelta(hours=2)
    late_marks={symbol:Mark(mark.price,late,late,mark.source) for symbol,mark in m.items()}
    pending=prepare_batch(s,'late',ds,late_marks,late,fees,steps)
    assert all(plan['quantity']=='0' and plan['reason']=='entry_plan_prepared_after_execution' for plan in pending['batches']['late']['plans'].values())
    result,report=apply_batch(pending,'late',o,late_marks,late)
    assert result['positions']=={} and not report['modeled_fills']


def test_late_preparation_retains_missing_exit_intent():
    s=state();s['cash']='90000';s['positions']={'SPY':'100'}
    ds,m,fees,steps,o=inputs(s,actions={'SPY':'exit','QQQ':'flat','IWM':'flat'})
    late=AT+timedelta(hours=2)
    late_marks={symbol:Mark(mark.price,late,late,mark.source) for symbol,mark in m.items()}
    pending=prepare_batch(s,'late-exit',ds,late_marks,late,fees,steps);o['SPY']=None
    result,report=apply_batch(pending,'late-exit',o,late_marks,late)
    assert result['pending_exits']==['SPY'] and result['positions']['SPY']=='100'


def test_late_equity_exit_cannot_backprice_valid_old_open():
    s=state();s['cash']='90000';s['positions']={'SPY':'100'}
    ds,m,fees,steps,o=inputs(s,actions={'SPY':'exit','QQQ':'flat','IWM':'flat'})
    late=AT+timedelta(hours=2)
    late_marks={symbol:Mark(mark.price,late,late,mark.source) for symbol,mark in m.items()}
    pending=prepare_batch(s,'late-exit',ds,late_marks,late,fees,steps)
    result,report=apply_batch(pending,'late-exit',o,late_marks,late)
    assert report['blocked']['SPY']=='missed_preparation_open'
    assert not report['modeled_fills'] and result['cash']=='90000'
    assert result['positions']['SPY']=='100' and result['pending_exits']==['SPY']


def test_crypto_late_exit_uses_fresh_quote_within_window():
    scheduled=datetime(2026,10,1,0,5,tzinfo=timezone.utc)
    later=scheduled+timedelta(seconds=30)
    s=state(CRYPTO);s['cash']='90000';s['positions']={'BTC/USD':'100'}
    ds,m,fees,steps,o=inputs(s,at=scheduled,actions={'BTC/USD':'exit','ETH/USD':'flat'})
    late_marks={symbol:Mark(mark.price,later,later,mark.source) for symbol,mark in m.items()}
    o={symbol:PriceObservation(later,later,bid=D(100),ask=D(100)) for symbol in o}
    pending=prepare_batch(s,'late-crypto-exit',ds,late_marks,later,fees,steps)
    result,report=apply_batch(pending,'late-crypto-exit',o,late_marks,later)
    assert len(report['modeled_fills'])==1 and report['modeled_fills'][0]['side']=='sell'
    assert 'BTC/USD' not in result['positions'] and not result['pending_exits']
