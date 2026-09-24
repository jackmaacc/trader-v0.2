from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import sqlite3

import pytest

from trader_engine.research.phase3_store import apply_batch, read_state


def args(path, ident='day1'):
    return dict(database=path, run_id='synthetic', registry_sha256='a'*64,
                implementation_sha256='b'*64, initial_state={'cash':'100000','halted':False},
                batch_id=ident, inputs={'loss_observed':True})


def latch(state, inputs):
    state['halted'] = state['halted'] or inputs['loss_observed']
    return {'state':state,'result':{'latched':state['halted']}}


def test_restart_replay_does_not_rerun_transition_or_unlatch(tmp_path):
    path=tmp_path/'research.sqlite3'
    first=apply_batch(**args(path),transition=latch)
    second_args=args(path,'day1-later');second_args['inputs']['loss_observed']=False
    apply_batch(**second_args,transition=latch)
    def never(*_): raise AssertionError('Replay called transition')
    repeat=apply_batch(**args(path),transition=never)
    assert first['inserted'] and not repeat['inserted']
    assert repeat['result_is_historical']
    assert repeat['state']['halted'] is True
    assert read_state(path)['batches']==2
    assert path.stat().st_mode & 0o077 == 0


def test_conflict_and_code_change_leave_state_unchanged(tmp_path):
    path=tmp_path/'state.db';apply_batch(**args(path),transition=latch)
    before=read_state(path)
    for changed in [dict(inputs={'loss_observed':False}),dict(implementation_sha256='c'*64),dict(initial_state={'cash':'1'})]:
        a=args(path);a.update(changed)
        with pytest.raises(ValueError): apply_batch(**a,transition=latch)
        assert read_state(path)==before


def test_failure_rolls_back_input_state_result_together(tmp_path):
    path=tmp_path/'state.db';apply_batch(**args(path),transition=latch)
    before=read_state(path)
    def fail(state, inputs):
        state['cash']='0';inputs.clear()
        raise RuntimeError('Injected crash before commit')
    a=args(path,'failed');original=deepcopy(a)
    with pytest.raises(RuntimeError):apply_batch(**a,transition=fail)
    assert a==original
    assert read_state(path)==before
    assert apply_batch(**a,transition=latch)['inserted']


@pytest.mark.parametrize('changed',[1,1.0])
def test_replay_preserves_json_types_even_when_python_equality_matches(tmp_path,changed):
    path=tmp_path/'state.db';apply_batch(**args(path),transition=latch)
    a=args(path);a['inputs']['loss_observed']=changed
    with pytest.raises(ValueError,match='Conflicting batch'):
        apply_batch(**a,transition=latch)
    assert read_state(path)['batches']==1


def test_two_writers_same_batch_apply_once(tmp_path):
    path=tmp_path/'state.db'
    # Initialize separately so this checks SQLite serialization, not directory creation.
    apply_batch(**args(path,'initial'),transition=latch)
    calls=[]
    def observe(state, inputs):
        calls.append(1)
        return latch(state,inputs)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:apply_batch(**args(path,'concurrent'),transition=observe),range(2)))
    assert sorted(r['inserted'] for r in results)==[False,True]
    assert len(calls)==1
    assert read_state(path)['batches']==2


@pytest.mark.parametrize('mutation',[
    "UPDATE research_batches SET body='{}' WHERE sequence=1",
    'DELETE FROM research_batches WHERE sequence=1',
    "UPDATE research_run SET sha256='bad'",
])
def test_corruption_blocks_read_and_append(tmp_path,mutation):
    path=tmp_path/'state.db'
    apply_batch(**args(path,'first'),transition=latch)
    apply_batch(**args(path,'second'),transition=latch)
    with sqlite3.connect(path) as c:c.execute(mutation)
    with pytest.raises(ValueError):read_state(path)
    with pytest.raises(ValueError):apply_batch(**args(path,'third'),transition=latch)


def test_foreign_schema_untouched(tmp_path):
    path=tmp_path/'foreign.db'
    with sqlite3.connect(path) as c:c.execute('CREATE TABLE user_data (value TEXT)')
    path.chmod(0o600)
    with pytest.raises(ValueError,match='Not an offline'):apply_batch(**args(path),transition=latch)
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()==[('user_data',)]


@pytest.mark.parametrize('output',[{'state':{},'result':{'price':float('nan')}},{'state':{}},{'state':[],'result':{}}])
def test_invalid_result_never_commits(tmp_path,output):
    path=tmp_path/'state.db';apply_batch(**args(path),transition=latch)
    before=read_state(path)
    with pytest.raises(ValueError):apply_batch(**args(path,'bad'),transition=lambda *_:output)
    assert read_state(path)==before


def test_read_missing_does_not_create_database_and_symlinks_rejected(tmp_path):
    path=tmp_path/'missing.db'
    with pytest.raises(sqlite3.OperationalError):read_state(path)
    assert not path.exists()
    target=tmp_path/'real.db';apply_batch(**args(target),transition=latch)
    path.symlink_to(target)
    with pytest.raises(ValueError):read_state(path)
    with pytest.raises(ValueError):apply_batch(**args(path),transition=latch)
