from datetime import datetime,timedelta,timezone
import json
import os
import pytest
from trader_engine.operations.paper_trial import PaperTrial,SOURCES,REQUIRED_SECONDS,sanitize

NOW=datetime(2026,9,24,15,tzinfo=timezone.utc)


def sources(root, now):
    for source in SOURCES:
        path=root/source;path.mkdir(parents=True,exist_ok=True)
        raw=dict(checked_at=now.isoformat(),status='running',connected=True,authenticated=True,subscribed=True,account={'cash':'1000','equity':'1000'},ownership={'BTC/USD':'0.1'})
        (path/'status.json').write_text(json.dumps(raw))
        if source=='paper_crypto_service': (path/'state.json').write_text(json.dumps({'ownership':raw['ownership'],'pending':None}))


def test_start_has_no_historic_credit_and_duplicate_heartbeats_are_idempotent(tmp_path):
    root=tmp_path/'artifacts';sources(root,NOW)
    trial=PaperTrial(tmp_path/'ledger')
    a=trial.poll(root,NOW)
    assert a['started_at']==NOW.isoformat() and a['continuous_seconds']==0
    assert trial.poll(root,NOW)==a
    assert trial.poll(root,NOW+timedelta(seconds=60))['continuous_seconds']==60
    assert trial.db.execute('select count(*) from observations').fetchone()[0]==5
    assert not a['live_approved'] and not a['investment_qualified']
    assert a['asset_status']['equities']=='research_only'
    trial.close()


def test_gap_boundary_and_restart_durable(tmp_path):
    root=tmp_path/'a';sources(root,NOW);directory=tmp_path/'l'
    trial=PaperTrial(directory);trial.poll(root,NOW)
    sources(root,NOW+timedelta(seconds=180))
    assert trial.poll(root,NOW+timedelta(seconds=180))['continuous_seconds']==180
    trial.close();trial=PaperTrial(directory)
    sources(root,NOW+timedelta(seconds=361))
    broken=trial.poll(root,NOW+timedelta(seconds=361))
    assert broken['streak_started_at'] is None and broken['gap_count']>=1
    sources(root,NOW+timedelta(seconds=421))
    restored=trial.poll(root,NOW+timedelta(seconds=421))
    assert restored['continuous_seconds']==0 and restored['gap_count']==broken['gap_count']
    assert trial.db.execute('select count(*) from gaps where ended_at is not null').fetchone()[0]>=1
    trial.close()


def test_one_heartbeat_each_day_cannot_qualify(tmp_path):
    root=tmp_path/'a';trial=PaperTrial(tmp_path/'l')
    for day in range(32):
        now=NOW+timedelta(days=day);sources(root,now)
        result=trial.poll(root,now)
        assert not result['operational_qualified']
    assert result['gap_count']>0
    trial.close()


def test_future_missing_and_regressed_timestamps_block(tmp_path):
    root=tmp_path/'a';sources(root,NOW+timedelta(seconds=1));trial=PaperTrial(tmp_path/'l')
    assert all(s['health']=='future_heartbeat' for s in trial.poll(root,NOW)['sources'])
    sources(root,NOW+timedelta(seconds=60));trial.poll(root,NOW+timedelta(seconds=60))
    sources(root,NOW+timedelta(seconds=59))
    assert all(s['health']=='heartbeat_regressed' for s in trial.poll(root,NOW+timedelta(seconds=61))['sources'])
    trial.close()


def test_only_allowlisted_evidence_persists(tmp_path):
    secret='DO-NOT-PERSIST-SECRET'
    raw=dict(status='running',error=secret,account={'cash':'1000','equity':'1000','secret':secret},ownership={'BTC/USD':'.1','API_KEY':secret},pending={'secret':secret},arbitrary=secret)
    assert secret not in json.dumps(sanitize('paper_crypto_service',raw))
    trial=PaperTrial(tmp_path/'l')
    assert os.stat(tmp_path/'l/observations.sqlite3').st_mode & 0o077 == 0
    assert trial.db.execute('PRAGMA journal_mode').fetchone()[0]=='wal'
    assert trial.db.execute('PRAGMA synchronous').fetchone()[0]==2
    trial.close()


def test_contiguous_thirty_days_only_qualifies_operations(tmp_path):
    # Exercise every observation window, never manufacture prior start metadata.
    root=tmp_path/'a';trial=PaperTrial(tmp_path/'l')
    trial.db.execute('PRAGMA synchronous=OFF') # Long logical soak; WAL durability is tested separately.
    for seconds in range(0,REQUIRED_SECONDS+1,180):
        now=NOW+timedelta(seconds=seconds);sources(root,now);result=trial.poll(root,now)
    assert result['continuous_seconds']==REQUIRED_SECONDS
    assert result['operational_qualified'] and not result['live_approved'] and not result['investment_qualified']
    trial.close()


def test_explicit_error_and_crypto_state_mismatch_are_preserved_gaps(tmp_path):
    root=tmp_path/'a';sources(root,NOW);trial=PaperTrial(tmp_path/'l')
    trial.poll(root,NOW)
    path=root/'continuous_market_scan/status.json';raw=json.loads(path.read_text());raw['errors']=['http_403'];path.write_text(json.dumps(raw))
    row=trial.poll(root,NOW+timedelta(seconds=60))
    assert row['gap_count']==1 and row['continuous_seconds']==0
    sources(root,NOW+timedelta(seconds=120))
    (root/'paper_crypto_service/state.json').write_text(json.dumps({'ownership':{'BTC/USD':'0.2'}}))
    row=trial.poll(root,NOW+timedelta(seconds=120))
    assert row['sources'][0]['health']=='state_status_disagreement'
    assert row['gap_count']==2
    trial.close()


def test_regular_pending_is_allowed_but_old_pending_is_not(tmp_path):
    root=tmp_path/'a';sources(root,NOW);trial=PaperTrial(tmp_path/'l')
    pending={'created_at':NOW.isoformat()}
    for name in ('status.json','state.json'):
        p=root/'paper_crypto_service'/name;row=json.loads(p.read_text());row['pending']=pending;p.write_text(json.dumps(row))
    assert trial.poll(root,NOW)['status']=='observing'
    now=NOW+timedelta(seconds=181);sources(root,now)
    for name in ('status.json','state.json'):
        p=root/'paper_crypto_service'/name;row=json.loads(p.read_text());row['pending']=pending;p.write_text(json.dumps(row))
    assert trial.poll(root,now)['sources'][0]['health']=='pending_reconciliation_stale'
    trial.close()


def test_malformed_status_does_not_stop_other_evidence(tmp_path):
    root=tmp_path/'a';sources(root,NOW);trial=PaperTrial(tmp_path/'l')
    path=root/'plus_research/status.json';raw=json.loads(path.read_text());raw.update(status=[],decisions=[{'action':[]}]);path.write_text(json.dumps(raw))
    result=trial.poll(root,NOW)
    assert result['status']=='interrupted'
    assert trial.db.execute('select count(*) from observations').fetchone()[0]==5
    trial.close()


def test_pending_identity_mismatch_interrupts_without_persisting_ids(tmp_path):
    root=tmp_path/'a';sources(root,NOW);trial=PaperTrial(tmp_path/'l')
    for name,identity in [('status.json','PRIVATE-ORDER-A'),('state.json','PRIVATE-ORDER-B')]:
        p=root/'paper_crypto_service'/name;row=json.loads(p.read_text())
        row['pending']={'created_at':NOW.isoformat(),'client_order_id':identity}
        p.write_text(json.dumps(row))
    result=trial.poll(root,NOW)
    assert result['sources'][0]['health']=='state_status_disagreement'
    assert 'PRIVATE-ORDER' not in str(trial.db.execute('select payload from observations').fetchall())
    trial.close()
