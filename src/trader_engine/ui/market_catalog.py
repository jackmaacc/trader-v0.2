from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd
import streamlit as st
from trader_engine.data.catalog import Instrument


def render_market_catalog(path: Path):
    with st.expander('Browse markets', expanded=True):
        if not path.exists():
            st.info('No market directory loaded yet. Market coverage will appear here after discovery.')
            return
        try:
            payload=json.loads(path.read_text())
            if payload.get('schema_version')!=1:raise ValueError('Unsupported directory format')
            for row in payload['instruments']:
                Instrument.model_validate({k:v for k,v in row.items() if k!='instrument_id'})
            frame=pd.DataFrame(payload['instruments'])
            observed=datetime.fromisoformat(payload['observed_at'])
            if observed.tzinfo is None: raise ValueError('Missing snapshot timezone')
            age=(datetime.now(timezone.utc)-observed).total_seconds()/3600
        except (OSError, ValueError, KeyError, TypeError) as exc:
            st.error(f'Market directory could not be loaded: {exc}')
            return
        st.caption(f"Directory snapshot: {observed:%Y-%m-%d %H:%M} UTC. These are listings, not live quotes or confirmed trading access.")
        if age>24: st.warning('This directory is more than a day old. Listings and contract availability may have changed.')
        st.warning('Worldwide coverage is incomplete. Historical prices and account access have not been verified.')
        if frame.empty:
            st.info('No instruments in this snapshot.')
            return
        counts=frame.groupby('kind').size().rename('Listings').reset_index().rename(columns={'kind':'Market'})
        st.dataframe(counts,hide_index=True,use_container_width=True)
        query=st.text_input('Find a symbol or instrument',key='catalog_search').strip()
        cols=st.columns(2)
        kinds=cols[0].multiselect('Markets',sorted(frame.kind.unique()),key='catalog_kinds')
        venues=cols[1].multiselect('Exchanges and venues',sorted(frame.venue.unique()),key='catalog_venues')
        if kinds:frame=frame.loc[frame.kind.isin(kinds)]
        if venues:frame=frame.loc[frame.venue.isin(venues)]
        if query:
            frame=frame.loc[frame.symbol.str.contains(query,case=False,regex=False)|frame.name.str.contains(query,case=False,regex=False)]
        st.caption(f'{len(frame):,} matching listings. Showing up to 500; use search to narrow the list.')
        shown=['symbol','name','kind','venue','currency','status','expiry','strike','option_right','contract_size','history_status']
        st.dataframe(frame.reindex(columns=shown).head(500),hide_index=True,use_container_width=True)
        st.download_button('Download matching directory',frame.to_json(orient='records',indent=2),
            file_name='market-directory.json',mime='application/json',key='catalog_download')
        with st.expander('Coverage and missing markets'):
            st.dataframe(pd.DataFrame(payload.get('coverage',[])),hide_index=True,use_container_width=True)
            for limitation in payload.get('limitations',[]):st.write(limitation)
