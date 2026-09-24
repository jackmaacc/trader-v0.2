"""Display saved capability evidence without broker access or activation controls."""
from pathlib import Path
import streamlit as st
from trader_engine.operations.capability_audit import build_capability_audit


def render_capability_audit(artifacts: Path):
    st.subheader('Market access and execution readiness')
    st.caption('Listings, data access, account permission and strategy approval are separate checks. This panel reads local evidence only.')
    try:
        snapshots=list((artifacts/'capability_evidence').glob('paper_account_*.json'))
        snapshot=max(snapshots,key=lambda p:p.stat().st_mtime) if snapshots else None
        report=build_capability_audit(artifacts,account_snapshot=snapshot)
    except (OSError,ValueError,TypeError,KeyError):
        st.warning('Capability evidence could not be verified. No readiness is inferred.')
        return
    rows=[]
    def display(value):
        return str(value).replace('_',' ')
    labels={'equities_etfs':'Stocks / ETFs','options':'Options','crypto':'Crypto','futures':'Futures','forex':'Forex'}
    def data_label(data):
        if 'feed' in data:
            subscription='subscription observed' if data.get('current_subscription_evidence') else 'subscription unverified'
            freshness=data.get('last_market_data',{}).get('state','unknown')
            return f"{data['feed'].upper()}: {subscription}; market data {display(freshness)}"
        return display(data.get('scope','unknown'))
    for name,entry in report.get('asset_classes',{}).items():
        rows.append({'Market':labels.get(name,name),'Data evidence':data_label(entry.get('market_data',{})),
                     'Account permission':display(entry.get('account_permission','unknown')),
                     'Execution implementation':display(entry.get('execution_adapter','unknown')),
                     'Strategy approval':display(entry.get('strategy_approval','unknown'))})
    if rows:st.dataframe(rows,hide_index=True,width='stretch')
    st.caption('Paper-account permission evidence does not establish live-account permission. Missing or expired evidence remains unverified; an authenticated stream may have no fresh market data.')
    with st.expander('Capability evidence and remaining blockers'):
        st.json(report)
