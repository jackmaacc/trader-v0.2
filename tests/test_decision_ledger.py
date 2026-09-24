import copy,json,sqlite3
import pytest
from trader_engine.operations.decision_ledger import record_decision,record_execution_link,verify_decisions


def args():
    return dict(decision_id='day1:SPY', strategy_id='E_DONCHIAN20_V1',decision_at='2026-10-01T13:20:00Z',
                registry_sha256='a'*64,code_sha256='b'*64,
                inputs=[dict(role='completed_bar',event_at='2026-09-30T20:00:00Z',received_at='2026-09-30T20:00:02Z',payload={'close':'100'})],result={'action':'entry_candidate','quantity':'80'})


def test_atomic_idempotent_inputs_and_fill_links(tmp_path):
    db=tmp_path.resolve()/'private'/'decisions.sqlite3'
    assert record_decision(db,**args())['inserted']
    assert not record_decision(db,**args())['inserted']
    assert db.stat().st_mode & 0o777==0o600
    link=dict(link_id='link1',decision_id='day1:SPY',client_order_id='client1',activity_ids=['partial1','partial2'],observed_at='2026-10-01T13:31:00Z',evidence_sha256='c'*64)
    assert record_execution_link(db,**link)['inserted']
    assert not record_execution_link(db,**link)['inserted']
    report=verify_decisions(db)
    assert (report['decisions'],report['inputs'],report['execution_links'])==(1,1,1)
    assert not report['broker_execution_verified']


def test_conflicts_and_amendments_preserve_original(tmp_path):
    db=tmp_path.resolve()/'d.db';record_decision(db,**args())
    changed=args();changed['result']['quantity']='40'
    with pytest.raises(ValueError,match='Conflicting'):record_decision(db,**changed)
    changed.update(decision_id='amended',amends='day1:SPY')
    record_decision(db,**changed)
    assert verify_decisions(db)['decisions']==2
    with sqlite3.connect(db) as c:
        old=json.loads(c.execute('SELECT body FROM decisions WHERE id=?',('day1:SPY',)).fetchone()[0])
    assert old['result']['quantity']=='80'


@pytest.mark.parametrize('bad', ['future_receipt','future_event','naive','missing','invalid_hash'])
def test_rejects_noncausal_or_unidentified_before_write(tmp_path,bad):
    values=args();db=tmp_path.resolve()/'d.db'
    if bad=='future_receipt':values['inputs'][0]['received_at']='2026-10-01T13:21:00Z'
    elif bad=='future_event':values['inputs'][0]['event_at']='2026-10-02T00:00:00Z'
    elif bad=='naive':values['decision_at']='2026-10-01T13:20:00'
    elif bad=='missing':values['inputs']=[]
    else:values['code_sha256']='unknown'
    with pytest.raises(ValueError):record_decision(db,**values)
    assert not db.exists()


@pytest.mark.parametrize('table', ['input_evidence','decisions','execution_links'])
def test_tampering_is_detected(tmp_path,table):
    db=tmp_path.resolve()/'d.db';record_decision(db,**args())
    record_execution_link(db,link_id='link',decision_id='day1:SPY',client_order_id='c',activity_ids=[],observed_at='2026-10-01T14:00:00Z',evidence_sha256='c'*64)
    with sqlite3.connect(db) as c:c.execute(f"UPDATE {table} SET body='{{}}'")
    with pytest.raises(ValueError):verify_decisions(db)


def test_no_link_without_decision_and_no_foreign_database_mutation(tmp_path):
    db=tmp_path.resolve()/'d.db';record_decision(db,**args())
    with pytest.raises(ValueError,match='earlier'):
        record_execution_link(db,link_id='link',decision_id='unknown',client_order_id='c',activity_ids=[],observed_at='2026-10-01T14:00:00Z',evidence_sha256='c'*64)
    assert verify_decisions(db)['execution_links']==0
    other=tmp_path.resolve()/'other.db'
    with sqlite3.connect(other) as c:c.execute('CREATE TABLE unrelated (x)')
    other.chmod(0o600)
    with pytest.raises(ValueError,match='Not a research'):record_decision(other,**args())
    with sqlite3.connect(other) as c:assert c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()==[('unrelated',)]


def test_rehashed_noncausal_links_and_cycles_fail_verification(tmp_path):
    from trader_engine.operations.decision_ledger import encoded,digest
    db=tmp_path.resolve()/'d.db';record_decision(db,**args())
    record_execution_link(db,link_id='link',decision_id='day1:SPY',client_order_id='c',activity_ids=[],observed_at='2026-10-01T14:00:00Z',evidence_sha256='c'*64)
    with sqlite3.connect(db) as c:
        row=json.loads(c.execute('SELECT body FROM execution_links').fetchone()[0]);row['observed_at']='2026-01-01T00:00:00Z'
        c.execute('UPDATE execution_links SET body=?,sha256=?',(encoded(row),digest(row)))
    with pytest.raises(ValueError,match='precedes'):verify_decisions(db)
    with sqlite3.connect(db) as c:
        c.execute('DELETE FROM execution_links')
        row=json.loads(c.execute('SELECT body FROM decisions').fetchone()[0]);row['amends']=row['id']
        c.execute('UPDATE decisions SET body=?,sha256=?',(encoded(row),digest(row)))
    with pytest.raises(ValueError,match='Cyclic'):verify_decisions(db)
