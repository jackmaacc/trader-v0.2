"""Read-only health panel for the separately supervised paper worker."""
import json
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st


def service_health(payload, now=None):
    """A saved running flag never overrides a stale or invalid heartbeat."""
    now = now or datetime.now(timezone.utc)
    try:
        checked = datetime.fromisoformat(payload['checked_at'].replace('Z', '+00:00'))
        age = (now - checked).total_seconds()
    except (KeyError, TypeError, ValueError):
        return 'unknown', 'No valid worker heartbeat is available.'
    if age < -30 or age > 180:
        return 'stale', 'Worker heartbeat is stale; automatic exits may be delayed.'
    if payload.get('mode') == 'observe_only':
        return 'attention', 'Observation mode only; automatic orders and exits are disabled.'
    if payload.get('error') or payload.get('status') in ('error', 'blocked', 'needs_attention', 'stopped', 'shutdown'):
        return 'attention', 'Worker requires attention: ' + str(payload.get('error') or payload.get('status'))
    return 'current', f'Latest worker check: {checked.isoformat()} ({age:.0f} seconds ago).'


@st.fragment(run_every='30s')
def render_crypto_service(artifacts_dir):
    root = Path(artifacts_dir)
    base = root if root.name == 'artifacts' else root.parent
    path = base / 'paper_crypto_service' / 'status.json'
    if not path.exists():
        return
    st.subheader('Paper crypto position manager')
    try:
        payload = json.loads(path.read_text())
        if not isinstance(payload, dict):
            raise ValueError('Invalid status')
    except (OSError, ValueError):
        st.error('Worker status could not be read; automatic management is unverified.')
        return
    health, message = service_health(payload)
    if health == 'current':
        st.success(message)
    else:
        st.error(message)
    st.write('BTC and ETH only. Completed daily close above the 200-day average: hold or enter. At or below: sell the owned position on the next successful worker check.')
    st.caption('Checks run every minute on the configured execution host. Sleep, lost connectivity or broker/data errors can delay exits. The daily rule is not an intraday stop. Research results remain unqualified.')
    if payload.get('decisions'):
        with st.expander('Latest decisions'):
            st.json(payload['decisions'])
    if payload.get('positions'):
        with st.expander('Last observed positions'):
            st.json(payload['positions'])
    st.caption('Status file: ' + str(path))
