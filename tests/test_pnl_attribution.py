from copy import deepcopy
from decimal import Decimal, localcontext
import pytest
from trader_engine.operations.accounting_ledger import EvidenceError, normalize_event
from trader_engine.operations.pnl_attribution import attribute

START='2026-09-23T00:00:00Z';END='2026-09-24T00:00:00Z'


def context(cash='1000',positions=None,endcash='1000',endpositions=None,multiplier='1'):
    return dict(start=START,end=END,currency='USD',instruments={'X':{'multiplier':multiplier}},
                opening={'timestamp':START,'cash':cash,'positions':positions or {}},
                ending={'timestamp':END,'cash':endcash,'positions':endpositions or {}},
                activities_complete=True,cashflows_complete=True,fees_complete=True,corporate_actions_complete=True)


def event(id,side,qty,price,fee='0',multiplier='1'):
    with localcontext() as c:
        c.prec=256
        return normalize_event(dict(id=str(id),activity_type='FILL',transaction_time=f'2026-09-23T12:00:{int(id):02d}Z',symbol='X',side=side,qty=qty,price=price,fee=fee),{'X':{'multiplier':multiplier}})


def evidence(opening=None,startmark=None,endmark=None):
    e={'opening_lots':opening or []}
    if startmark is not None:e['opening_marks']={'X':{'price':startmark,'timestamp':START}}
    if endmark is not None:e['terminal_marks']={'X':{'price':endmark,'timestamp':END}}
    return e


def test_fifo_partial_close_and_remainder_fee():
    c=context(endcash='985.3',endpositions={'X':'1'})
    rows=[event(1,'buy','2','10','.2'),event(2,'buy','1','20','.1'),event(3,'sell','2','13','.4')]
    r=attribute(c,rows,evidence(endmark='21'))
    assert r['status']=='attributed'
    m=r['metrics'];assert Decimal(m['realized_gross'])==Decimal('6');assert Decimal(m['realized_net'])==Decimal('5.4')
    assert Decimal(m['ending_unrealized_net'])==Decimal('0.9');assert Decimal(m['period_net_pnl'])==Decimal('6.3');assert Decimal(m['identity_difference'])==Decimal('0.0')


def test_short_reversal_then_cover_option_multiplier():
    rows=[event(1,'sell','2','3','2','100'),event(2,'buy','3','2','3','100'),event(3,'sell','1','2.5','1','100')]
    r=attribute(context(endcash='1244',multiplier='100'),rows,evidence())
    assert r['status']=='attributed';assert Decimal(r['metrics']['realized_gross'])==Decimal('250.0');assert Decimal(r['metrics']['realized_net'])==Decimal('244.0')


def test_opening_basis_distinct_from_period_mark_pnl():
    lots=[{'symbol':'X','quantity':'2','price':'5','entry_fee_remaining':'.2','acquired_at':'2026-09-22T00:00:00Z'}]
    c=context(positions={'X':'2'},endcash='1013.8',endpositions={'X':'1'})
    r=attribute(c,[event(1,'sell','1','14','.2')],evidence(lots,'10','12'))
    assert r['status']=='attributed';assert Decimal(r['metrics']['realized_net'])==Decimal('8.7')
    assert Decimal(r['metrics']['period_net_pnl'])==Decimal('5.8') # 4 close +2 remaining minus .2 new fee


def test_income_cashflow_standalone_fee_separate():
    c=context(endcash='1041')
    rows=[]
    for i,(kind,cash) in enumerate([('CSD','50'),('CSW','-10'),('DIV','2'),('INT','1'),('FEE','-2')]):
        rows.append(normalize_event(dict(id=str(i),activity_type=kind,transaction_time=f'2026-09-23T12:00:0{i}Z',net_amount=cash),{}))
    r=attribute(c,rows,evidence());assert r['status']=='attributed'
    assert Decimal(r['metrics']['external_cashflow'])==Decimal('40');assert Decimal(r['metrics']['income'])==Decimal('3');assert Decimal(r['metrics']['period_net_pnl'])==Decimal('1')


def test_fee_thirds_preserve_residual_exactly():
    rows=[event(1,'buy','3','1','.01'),event(2,'sell','1','1'),event(3,'sell','1','1'),event(4,'sell','1','1')]
    r=attribute(context(endcash='999.99'),rows,evidence())
    assert r['status']=='attributed';assert Decimal(r['metrics']['realized_net'])==Decimal('-0.010000000000')
    assert r['matches'][-1]['fees']=='0.003333333334'


@pytest.mark.parametrize('change',['basis','openingmark','terminalmark','fees','corporate'])
def test_missing_evidence_never_claims_attribution(change):
    c=context(positions={'X':'1'},endpositions={'X':'1'})
    lot=dict(symbol='X',quantity='1',price='5',entry_fee_remaining='0',acquired_at=START)
    e=evidence([lot],'10','10')
    if change=='basis':e['opening_lots']=[]
    elif change=='openingmark':del e['opening_marks']
    elif change=='terminalmark':del e['terminal_marks']
    elif change=='fees':c['fees_complete']=False
    else:c['corporate_actions_complete']=False
    assert attribute(c,[],e)['status']=='inconclusive'


def test_unsupported_basis_adjustment_is_inconclusive():
    row=dict(id='1',timestamp='2026-09-23T12:00:00Z',kind='POSITION_ADJUSTMENT',symbol='X',quantity='1',cash='0',fee='0',external_cashflow='0')
    assert attribute(context(),[row],evidence())['metrics'] is None


def test_ambiguous_fill_order_needs_explicit_order():
    rows=[event(1,'buy','1','1'),event(2,'sell','1','2')];rows[1]['timestamp']=rows[0]['timestamp']
    assert attribute(context(endcash='1001'),rows,evidence())['status']=='inconclusive'
    e=evidence();e['event_order']=['1','2'];assert attribute(context(endcash='1001'),rows,e)['status']=='attributed'


def test_inaccurate_ending_cash_is_mismatch():
    r=attribute(context(endcash='999'),[],evidence());assert r['status']=='mismatch';assert Decimal(r['metrics']['identity_difference'])==Decimal('-1')


def test_mark_timestamp_cannot_be_invented_or_stale():
    e=evidence(endmark='1');e['terminal_marks']['X']['timestamp']=START
    with pytest.raises(EvidenceError,match='exact boundary'):
        attribute(context(endcash='999',endpositions={'X':'1'}),[event(1,'buy','1','1')],e)


def test_readonly_database_adapter_preserves_source(tmp_path):
    import json
    from trader_engine.operations.accounting_ledger import import_bundle
    from trader_engine.operations.pnl_attribution import attribute_database
    c=context(endcash='990',endpositions={'X':'1'});c['account_reference']='fixture'
    raw=[dict(id='1',activity_type='FILL',transaction_time='2026-09-23T12:00:01Z',symbol='X',side='buy',qty='1',price='10')]
    a=tmp_path/'a.json';a.write_text(json.dumps(raw));p=tmp_path/'c.json';p.write_text(json.dumps(c));db=tmp_path/'ledger.sqlite'
    import_bundle(db,a,p);before=db.read_bytes()
    r=attribute_database(db,evidence(endmark='11'))
    assert r['status']=='attributed' and r['sources'];assert db.read_bytes()==before


def test_invalid_normalized_cashflows_rejected():
    row=dict(id='1',timestamp='2026-09-23T12:00:00Z',kind='CSD',symbol=None,quantity='0',cash='10',fee='0',external_cashflow='0')
    with pytest.raises(EvidenceError,match='deposit'):attribute(context(endcash='1010'),[row],evidence())


def test_opening_short_partial_cover_and_terminal_liability():
    lot=dict(symbol='X',quantity='-2',price='10',entry_fee_remaining='.2',acquired_at=START)
    c=context(positions={'X':'-2'},endcash='992.8',endpositions={'X':'-1'})
    r=attribute(c,[event(1,'buy','1','7','.2')],evidence([lot],'8','6'))
    assert r['status']=='attributed'
    assert Decimal(r['metrics']['period_net_pnl'])==Decimal('2.8')
    assert Decimal(r['metrics']['ending_unrealized_net'])==Decimal('3.9')
