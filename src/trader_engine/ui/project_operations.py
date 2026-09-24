"""Project delivery and paper-trial progress, independent of trading authority."""
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import streamlit as st


def load(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


@st.fragment(run_every='30s')
def render_project_operations(artifacts: Path):
    st.subheader('Project roadmap and one-month paper trial')
    st.caption('Mac development and paper operation now; Windows/WSL2 runtime and private Mac access after a verified handoff. Only one account executor may run.')
    status = load(artifacts/'paper_trial/status.json')
    if not status:
        st.info('Paper-trial ledger has not reported yet. No elapsed time is credited before recording starts.')
    else:
        try:
            age = (datetime.now(timezone.utc)-datetime.fromisoformat(status['checked_at'])).total_seconds()
            stale = not 0 <= age <= 180
        except (KeyError, TypeError, ValueError):
            stale = True
        if stale:
            st.warning('Paper-trial observer is stale; its saved progress is not current qualification.')
        seconds = status.get('continuous_seconds', 0)
        required = 30*86400
        try:
            seconds, required = float(seconds), float(required)
            if not math.isfinite(seconds) or not math.isfinite(required) or required <= 0:
                raise ValueError('invalid trial duration')
            progress = max(0., min(1., seconds/required))
        except (ValueError, TypeError, ZeroDivisionError):
            progress = 0.
        st.progress(progress, text=f'Operational continuity: {progress*30:.2f} / 30 days')
        st.write(f"Observer: {status.get('status', 'unknown')} · Started: {status.get('started_at', 'unknown')} · Recorded gaps: {status.get('gap_count', 0)}")
        st.caption(f"Current streak began: {status.get('streak_started_at', 'not yet established')}")
        if status.get('operational_qualified') is True and not stale and status.get('status') == 'observing' and progress >= 1:
            st.success('Minimum operational continuity met. Investment qualification and live activation remain separate.')
        sources = status.get('sources', [])
        if isinstance(sources, list) and sources:
            st.dataframe(sources, hide_index=True, width='stretch')
        assets = status.get('asset_status', {})
        st.write('Asset-class evidence:', assets)
    st.warning('Live trading is disabled. Thirty days of process health is not thirty days of equity/options paper trading or evidence of profitability.')
    st.caption('Equities/ETFs and options remain separate research workstreams. The current broker writer is the BTC/ETH experiment. Saved account equity is not cash-flow-adjusted performance.')
    with st.expander('Delivery milestones'):
        st.markdown('1. Mandate and component inventory\n2. Portable operations and remote-access preparation\n3. Durable research and broker accounting\n4. Parallel equity/ETF, options and crypto research\n5. Unified execution and portfolio risk\n6. Prospective paper qualification\n7. Explicit small live pilot and reviewed expansion')
