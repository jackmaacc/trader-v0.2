"""Offline daily adapter-to-evidence integration; no prospective credit or broker I/O."""
from dataclasses import asdict
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path

from trader_engine.operations.decision_ledger import record_decision, verify_decisions
from . import phase3_daily
from .protocol_registry import registry_hash

SUPPORTED_REGISTRY = 'b33d9064bb674f3948ac4b004a3ec88ad4553e08236ad42e5278741a6f4bedf0'


def _jsonable(value):
    if isinstance(value, (date, datetime, Decimal)):
        return value.isoformat() if isinstance(value, (date,datetime)) else str(value)
    if isinstance(value, dict): return {k:_jsonable(v) for k,v in value.items()}
    if isinstance(value, (tuple,list)): return [_jsonable(v) for v in value]
    return value


def record_daily_snapshot(snapshot, registry, database):
    """Evaluate one explicit saved snapshot and retain its complete decision inputs.

    Unlike the lower-level signal adapter, this evidence interface refuses a bundle
    containing later-received bars. It cannot turn today's historical download into
    past prospective evidence: all outputs are marked offline_reconstruction.
    """
    protocol_hash=registry_hash(registry)
    if protocol_hash != SUPPORTED_REGISTRY:
        raise ValueError('Adapter does not implement this changed registry')
    strategy=snapshot['strategy_id']
    if strategy not in [t['id'] for t in registry['tracks']]:
        raise ValueError('Strategy absent from registry')
    def time(value):
        result=datetime.fromisoformat(value.replace('Z','+00:00'))
        if result.tzinfo is None: raise ValueError('Timezone required')
        return result.astimezone(timezone.utc)
    decision_at=time(snapshot['decision_at'])
    metadata_received=time(snapshot['metadata_received_at'])
    if metadata_received>decision_at: raise ValueError('Metadata unavailable at decision')
    calendar=[phase3_daily.Session(date.fromisoformat(s['day']),time(s['open_at']),time(s['close_at'])) for s in snapshot['calendar']]
    days={s.day:s for s in calendar}
    bars=[];sources=[]
    for b in snapshot['bars']:
        bar=phase3_daily.DailyBar(b['symbol'],date.fromisoformat(b['day']),Decimal(b['raw_close']),
                                None if b.get('total_return_close') is None else Decimal(b['total_return_close']),time(b['received_at']))
        if bar.day not in days or bar.received_at>decision_at or days[bar.day].close_at>bar.received_at:
            raise ValueError('Unavailable or incomplete bar in evidence snapshot')
        bars.append(bar)
        sources.append(dict(role='completed_daily_bar',event_at=days[bar.day].close_at.isoformat(),received_at=bar.received_at.isoformat(),payload=b))
    if not bars: raise ValueError('Snapshot needs retained bar evidence')
    metadata={k:v for k,v in snapshot.items() if k!='bars'}
    sources.append(dict(role='calendar_and_position_input',event_at=metadata_received.isoformat(),received_at=metadata_received.isoformat(),payload=metadata))
    signal=phase3_daily.decide_signal(strategy,snapshot['symbol'],bars,calendar,date.fromisoformat(snapshot['signal_day']),decision_at,
                                    held_quantity=Decimal(snapshot['held_quantity']),pending_exit=snapshot['pending_exit'])
    result=dict(signal=_jsonable(asdict(signal)),mode='offline_reconstruction',prospective_credit=False,execution_authorized=False)
    # Bind both the actual adapter and this conversion layer, not a caller's label.
    source_hashes={p.name:sha256(p.read_bytes()).hexdigest() for p in [Path(phase3_daily.__file__),Path(__file__)]}
    code_hash=sha256(json.dumps(source_hashes,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    result['source_hashes']=source_hashes
    receipt=record_decision(database,decision_id=snapshot['decision_id'],strategy_id=strategy,
                            decision_at=decision_at.isoformat(),registry_sha256=protocol_hash,code_sha256=code_hash,
                            inputs=sources,result=result)
    return {'decision':result,'receipt':receipt,'ledger':verify_decisions(database)}
