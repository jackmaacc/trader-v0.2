import json
import sqlite3
from copy import deepcopy

import pytest

from trader_engine.operations.accounting_ledger import EvidenceError, import_bundle, reconcile


def context():
    return dict(account_reference='paper-fixture', currency='USD', start='2026-09-23T00:00:00Z',
                end='2026-09-24T00:00:00Z', instruments={'ETF': {'multiplier': '1'}, 'OPTION': {'multiplier': '100'}},
                opening={'timestamp': '2026-09-23T00:00:00Z', 'cash': '1000', 'positions': {}},
                ending={'timestamp': '2026-09-24T00:00:00Z', 'cash': '979.90', 'positions': {'ETF': '2'}},
                activities_complete=True, cashflows_complete=True, fees_complete=True, corporate_actions_complete=True)


def fill(id='1', **extra):
    return dict(id=id, activity_type='FILL', transaction_time='2026-09-23T12:00:00Z',
                symbol='ETF', side='buy', qty='2', price='10', fee='0.10', **extra)


def run(tmp_path, rows, config=None):
    a, c = tmp_path / 'activities.json', tmp_path / 'context.json'
    a.write_text(json.dumps(rows)); c.write_text(json.dumps(config or context()))
    return import_bundle(tmp_path / 'ledger.sqlite', a, c)


def test_exact_accounting_repeat_and_provenance(tmp_path):
    r = run(tmp_path, [fill()]); assert r['status'] == 'reconciled'
    assert r['fees'] == '0.10' and r['new_events'] == 1
    assert run(tmp_path, [fill(), fill()])['new_events'] == 0
    r = reconcile(tmp_path / 'ledger.sqlite')
    assert r['event_count'] == 1 and len(r['sources']) == 2
    assert not r['return_calculated'] and not r['execution_authorized']


def test_conflicting_duplicate_rolls_back_entire_batch(tmp_path):
    run(tmp_path, [fill()])
    bad = fill(); bad['price'] = '11'
    with pytest.raises(EvidenceError, match='Conflicting'):
        run(tmp_path, [fill('new'), bad])
    assert reconcile(tmp_path / 'ledger.sqlite')['event_count'] == 1


def test_partial_fills_not_cumulative_order_qty(tmp_path):
    a, b = fill(), fill('2')
    for row in (a,b): row.update(qty='1', fee='0.05', order_id='same', cum_qty='2', order_status='canceled')
    assert run(tmp_path,[a,b])['status'] == 'reconciled'


def test_option_short_cashflow_fee_and_close(tmp_path):
    c = context(); c['ending'].update(cash='1059.95', positions={})
    a = fill(); a.update(symbol='OPTION', side='sell', qty='1',price='1.20',fee='0')
    b = deepcopy(a); b.update(id='2',side='buy',price='0.90')
    rows=[a,b,dict(id='3',activity_type='CSD',transaction_time=a['transaction_time'],net_amount='30'),
          dict(id='4',activity_type='FEE',transaction_time=a['transaction_time'],net_amount='-0.05')]
    r=run(tmp_path,rows,c); assert r['status']=='reconciled'; assert r['external_cashflow']=='30'


@pytest.mark.parametrize('field,value', [('qty','NaN'),('price','Infinity'),('qty','-1'),('fee','-1'),('side','bogus')])
def test_invalid_fill(tmp_path,field,value):
    row=fill(); row[field]=value
    with pytest.raises(EvidenceError): run(tmp_path,[row])


def test_missing_multiplier_unknown_activity_and_outside_window(tmp_path):
    for changes in ({'symbol':'MISSING'}, {'activity_type':'SPLIT'}, {'transaction_time':'2026-09-23T00:00:00Z'}, {'transaction_time':'2026-09-23T12:00:00'}):
        row=fill(); row.update(changes)
        with pytest.raises(EvidenceError):run(tmp_path,[row])
    assert not (tmp_path/'ledger.sqlite').exists()


def test_incomplete_evidence_cannot_pass_empty_balances(tmp_path):
    c=context(); c['ending'].update(cash='1000',positions={}); c['activities_complete']=False
    assert run(tmp_path,[],c)['status']=='inconclusive'


def test_missing_snapshots_inconclusive(tmp_path):
    c=context(); del c['opening']
    r=run(tmp_path,[fill()],c);assert r['status']=='inconclusive'; assert 'opening_snapshot' in r['missing_evidence']


def test_mismatch_is_explicit_even_when_incomplete(tmp_path):
    c=context();c['ending']['cash']='980';c['fees_complete']=False
    r=run(tmp_path,[fill()],c);assert r['status']=='mismatch';assert r['cash_difference']=='-0.10'


def test_context_change_rejected(tmp_path):
    run(tmp_path,[fill()]);c=context();c['account_reference']='other'
    with pytest.raises(EvidenceError,match='Context differs'):run(tmp_path,[fill()],c)


def test_source_cannot_be_destination(tmp_path):
    p=tmp_path/'source.json';p.write_text('[]')
    with pytest.raises(EvidenceError,match='overwrite'):import_bundle(p,p,p)
    assert p.read_text()=='[]'


def test_boundary_snapshot_required(tmp_path):
    c=context();c['opening']['timestamp']='2026-09-22T00:00:00Z'
    with pytest.raises(EvidenceError,match='boundaries'):run(tmp_path,[fill()],c)


def test_database_private_and_symlink_rejected(tmp_path):
    run(tmp_path, [fill()])
    assert (tmp_path/'ledger.sqlite').stat().st_mode & 0o777 == 0o600
    target=tmp_path/'target';target.write_text('untouched')
    (tmp_path/'ledger.sqlite').unlink();(tmp_path/'ledger.sqlite').symlink_to(target)
    with pytest.raises(EvidenceError,match='Symlink'):run(tmp_path,[fill()])
    assert target.read_text()=='untouched'


def test_permissive_existing_db_refused(tmp_path):
    run(tmp_path,[fill()]);(tmp_path/'ledger.sqlite').chmod(0o644)
    with pytest.raises(EvidenceError,match='owner-only'):run(tmp_path,[fill()])


def test_large_product_and_tiny_fee_remain_exact(tmp_path):
    row=fill();row.update(qty='1000000000000',price='1000000000000',fee='0.000000000001')
    c=context();c['instruments']['ETF']['multiplier']='1000000000000'
    r=run(tmp_path,[row],c)
    assert r['expected_cash']=='-999999999999999999999999999999999000.000000000001'
    assert r['status']=='mismatch'


@pytest.mark.parametrize('value',['1000000000001','0.0000000000001'])
def test_unsupported_input_range_rejected(tmp_path,value):
    row=fill();row['qty']=value
    with pytest.raises(EvidenceError,match='supported bound'):run(tmp_path,[row])


def test_new_nested_parent_directories_private(tmp_path):
    a=tmp_path/'a.json'; c=tmp_path/'c.json'
    a.write_text(json.dumps([fill()]));c.write_text(json.dumps(context()))
    db=tmp_path/'new'/'nested'/'ledger.sqlite'
    assert import_bundle(db,a,c)['status']=='reconciled'
    assert db.parent.stat().st_mode & 0o777 == 0o700
    assert db.parent.parent.stat().st_mode & 0o777 == 0o700
