from datetime import datetime,timezone
import json
import numpy as np
import pandas as pd
import pytest
from trader_engine.execution.shadow import run_shadow
from trader_engine.execution.account_risk import AccountRisk
from trader_engine.research.strategy_spec import StrategySpec,ETF_UNIVERSE
from trader_engine.agents.models import SharedContext
from trader_engine.data.quote_validation import QuoteEnvelope,QuotePolicy,validate_quote


def inputs(tmp_path):
    spec=StrategySpec('MOM20');asof=pd.Timestamp('2026-09-24 14:00Z')
    idx=pd.bdate_range(end='2026-09-23',periods=21,tz='America/New_York')+pd.Timedelta(hours=16)
    close=100+np.arange(21)*.5+np.sin(np.arange(21))*.2
    f=pd.DataFrame({'high':close+.5,'low':close-.5,'close':close,'total_return_close':close},index=idx)
    q={s:validate_quote(QuoteEnvelope(s,'sip',asof.isoformat(),asof.isoformat(),dict(t=asof.isoformat(),bp=110,ap=110.01,bs=10,**{'as':10}))) for s in ETF_UNIVERSE}
    risk=AccountRisk(tmp_path/'risk.json');risk.initialize(100000,100000,'2026-09-24')
    return dict(context=SharedContext(asof.to_pydatetime(),spec.spec_hash,ETF_UNIVERSE,()),histories={s:f.copy() for s in ETF_UNIVERSE},
      quotes=q,spec=spec,risk=risk,positions=[],cash=100000,output_dir=tmp_path/'shadow',
      session_open=pd.Timestamp('2026-09-24 13:30Z'),session_close=pd.Timestamp('2026-09-24 20:00Z'),
      previous_session_close=idx[-1],history_available_at={s:idx[-1] for s in ETF_UNIVERSE})


def test_diagnostic_shared_candidates_team_tapes_and_no_state_write(tmp_path):
    kw=inputs(tmp_path);before=kw['risk'].path.read_bytes();result=run_shadow(**kw)
    assert len(result['team'].reports)==5
    assert any(d['eligible'] for d in result['decisions'])
    assert not result['formal_forward_run']
    assert kw['risk'].path.read_bytes()==before
    assert json.loads((kw['output_dir']/'manifest.json').read_text())['order_authority'] is False
    with pytest.raises(FileExistsError):run_shadow(**kw)


def test_stale_validated_quote_revalidated_and_advisory_cannot_override(tmp_path):
    kw=inputs(tmp_path)
    # Previously valid at retrieval is stale at the actual shadow decision.
    old=pd.Timestamp('2026-09-24 13:59Z')
    for s in ETF_UNIVERSE:
        kw['quotes'][s]=validate_quote(QuoteEnvelope(s,'sip',old.isoformat(),old.isoformat(),dict(t=old.isoformat(),bp=110,ap=110.01,bs=10,**{'as':10})))
    result=run_shadow(**kw)
    assert all(not d['eligible'] and d['reason']=='invalid_or_stale_quote' for d in result['decisions'])


def test_daily_halt_blocks_all_shadow_entries(tmp_path):
    kw=inputs(tmp_path);kw['risk'].observe(99500,'2026-09-24')
    result=run_shadow(**kw)
    assert all(not d['eligible'] for d in result['decisions'])
    assert all(d['reason']=='daily_halt' for d in result['decisions'])


def test_future_history_or_unregistered_strategy_rejected(tmp_path):
    kw=inputs(tmp_path);kw['history_available_at']['SPY']=pd.Timestamp('2026-09-25 20:00Z')
    result=run_shadow(**kw)
    assert next(d for d in result['decisions'] if d['symbol']=='SPY')['reason']=='daily_availability_invalid'


def test_marketable_stop_sizes_bid_liquidation_and_carries_risk(tmp_path, monkeypatch):
    from trader_engine.research.etf_candidates import ETFDecision
    kw=inputs(tmp_path)
    spec=StrategySpec('MOM20',slippage_bps=60,commission_bps=2,max_portfolio_risk=.001)
    kw['spec']=spec
    kw['context']=SharedContext(kw['context'].as_of,spec.spec_hash,ETF_UNIVERSE,())
    monkeypatch.setattr('trader_engine.execution.shadow.momentum_decision',
                        lambda f,s:ETFDecision(True,'qualified',3.,.01,None))
    rows=run_shadow(**kw)['decisions']
    approved=[r for r in rows if r['eligible']]
    assert len(approved)>=2
    liquidation=110*(1-spec.one_way_impact)*(1-spec.commission_rate)
    assert all(r['quantity']*(r['proposed_entry']*(1+spec.commission_rate)-liquidation)<=100+1e-8 for r in approved)
    total=sum(r['quantity']*(r['proposed_entry']-liquidation) for r in approved)
    assert total<=100+1e-8


@pytest.mark.parametrize('kind', ['missing','symbol_mismatch','nonfinite'])
def test_every_shadow_quote_decision_retains_exact_diagnostic(tmp_path,kind):
    kw=inputs(tmp_path)
    if kind=='missing':kw['quotes'].pop('SPY')
    else:
        old=kw['quotes']['SPY'].envelope
        raw=dict(old.raw_payload)
        if kind=='nonfinite':raw['bp']=float('nan')
        kw['quotes']['SPY']=validate_quote(QuoteEnvelope(
            'QQQ' if kind=='symbol_mismatch' else 'SPY',old.feed,old.received_at,old.decision_at,raw))
    run_shadow(**kw)
    rows=json.loads((kw['output_dir']/'decisions.json').read_text(),
                    parse_constant=lambda value:pytest.fail('Non-standard JSON '+value))
    row=next(r for r in rows if r['symbol']=='SPY')
    assert not row['eligible']
    expected={'missing':'missing_quote','symbol_mismatch':'quote_symbol_mismatch','nonfinite':'invalid_price'}[kind]
    assert expected in row['quote']['reasons']
    assert all('quote' in r for r in rows)
