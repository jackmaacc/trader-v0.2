"""Operational visibility for Plus data and research, without broker controls."""
from datetime import datetime, timezone
import json
from pathlib import Path
import pandas as pd
import streamlit as st


def read(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


@st.fragment(run_every='15s')
def render_plus_capabilities(artifacts: Path):
    st.subheader('Algo Trader Plus: data to decisions')
    st.caption('Streams and research evaluate observations. Only the separate BTC/ETH paper worker currently submits orders.')
    rows = []
    for name, folder in [('SIP stream', 'plus_stream'), ('OPRA stream', 'plus_options_stream'), ('Signal and options evaluation', 'plus_research')]:
        status = read(artifacts/folder/'status.json')
        try:
            age = (datetime.now(timezone.utc)-datetime.fromisoformat(status['checked_at'])).total_seconds()
            health = status.get('status', 'unknown') if 0 <= age <= 180 else 'stale'
        except (KeyError, ValueError, TypeError):
            health = 'not reporting'
        rows.append(dict(component=name, status=health, checked_at=status.get('checked_at'),
                         data_state=status.get('data_state'), error=status.get('error') or status.get('errors')))
    st.dataframe(pd.DataFrame(rows).astype(str), hide_index=True, width='stretch')
    decisions = read(artifacts/'plus_research/decisions.json')
    if decisions.get('records'):
        st.write('Latest equity decisions')
        records = [{**r, 'reasons': '; '.join(r.get('reasons', [])), 'signal': str(r.get('signal'))} for r in decisions['records']]
        st.dataframe(pd.DataFrame(records), hide_index=True, width='stretch')
    else:
        st.info('Waiting for the first recorded decision cycle.')
    options = read(artifacts/'plus_research/options.json')
    st.caption('OPRA options: liquidity and data-quality diagnostics only. Contract eligibility, expiry and order management are not approved for execution.')
    st.caption(f"Saved options assessment at {options.get('checked_at', 'not available')}; quality labels describe that assessment time, not current quote validity.")
    if options.get('records'):
        records = []
        for original in options['records']:
            row = dict(original)
            try:
                age = (datetime.now(timezone.utc)-datetime.fromisoformat(str(row.get('quote_asof')).replace('Z', '+00:00'))).total_seconds()
                if not 0 <= age <= 60:
                    row['quality_status'] = 'stale_saved_assessment'
            except (ValueError, TypeError):
                row['quality_status'] = 'unknown_quote_age'
            records.append(row)
        st.dataframe(pd.DataFrame(records).astype(str), hide_index=True, width='stretch')
    for source in options.get('sources', []):
        st.caption(f"{source.get('underlying')}: assessed {source.get('assessed_count', '?')} of {source.get('input_count', '?')} returned snapshots; assessment truncated: {source.get('assessment_truncated', 'unknown')}.")
        if not source.get('complete') or source.get('errors'):
            st.warning(f"Options coverage incomplete: {source.get('underlying')}: {source.get('errors')}")
