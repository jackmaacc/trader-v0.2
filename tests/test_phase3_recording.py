import copy,json,sqlite3
from pathlib import Path
from datetime import date,datetime,time,timedelta,timezone
import pytest
from trader_engine.research.phase3_recording import record_daily_snapshot


def snapshot():
    # Synthetic session fixture, never a claim about an actual exchange calendar.
    days=[]; d=date(2026,8,1)
    while len(days)<22:
        if d.weekday()<5: days.append(d)
        d+=timedelta(days=1)
    sessions=[dict(day=d.isoformat(),open_at=datetime.combine(d,time(13,30),timezone.utc).isoformat(),close_at=datetime.combine(d,time(20),timezone.utc).isoformat()) for d in days]
    bars=[dict(symbol='SPY',day=d.isoformat(),raw_close='101' if i==20 else '100',total_return_close='101' if i==20 else '100',received_at=datetime.combine(d,time(20,1),timezone.utc).isoformat()) for i,d in enumerate(days[:21])]
    return dict(decision_id='synthetic:SPY',strategy_id='E_DONCHIAN20_V1',symbol='SPY',signal_day=days[20].isoformat(),
                decision_at=datetime.combine(days[21],time(13,20),timezone.utc).isoformat(),metadata_received_at='2026-08-01T00:00:00Z',
                held_quantity='0',pending_exit=False,calendar=sessions,bars=bars)


def registry():
    return json.loads((Path(__file__).resolve().parents[1]/'config/research/phase3_protocols.json').read_text())


def test_actual_adapter_result_and_sources_are_durably_linked(tmp_path):
    db=tmp_path.resolve()/'ledger.db';data=snapshot()
    report=record_daily_snapshot(data,registry(),db)
    assert report['decision']['signal']['action']=='enter'
    assert report['decision']['mode']=='offline_reconstruction'
    assert report['decision']['prospective_credit'] is False
    assert report['ledger']['inputs']==22
    assert report['ledger']['decisions']==1
    assert not record_daily_snapshot(data,registry(),db)['receipt']['inserted']
    with sqlite3.connect(db) as c:
        body=json.loads(c.execute('SELECT body FROM decisions').fetchone()[0])
    assert body['result']==report['decision']
    assert len(body['input_sha256'])==22


def test_later_received_input_cannot_be_backfilled_as_known(tmp_path):
    data=snapshot();data['bars'][0]['received_at']='2026-10-01T00:00:00Z'
    db=tmp_path.resolve()/'ledger.db'
    with pytest.raises(ValueError,match='Unavailable'):record_daily_snapshot(data,registry(),db)
    assert not db.exists()


def test_no_signal_decisions_are_retained(tmp_path):
    data=snapshot();data['bars'][-1]['total_return_close']='100'
    report=record_daily_snapshot(data,registry(),tmp_path.resolve()/'ledger.db')
    assert report['decision']['signal']['action']!='enter'
    assert report['ledger']['decisions']==1


def test_changed_registry_cannot_label_unchanged_adapter(tmp_path):
    changed=registry();changed['tracks'][0]['rules']['signal']='Use a different lookback'
    with pytest.raises(ValueError,match='changed registry'):
        record_daily_snapshot(snapshot(),changed,tmp_path.resolve()/'ledger.db')
