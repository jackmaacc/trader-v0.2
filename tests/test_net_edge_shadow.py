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
