"""Read-only views for frozen research and the five advisory specialists."""
from pathlib import Path
import json
import streamlit as st
import pandas as pd

def _read(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None

def find_net_edge_root(artifacts_dir):
    root = Path(artifacts_dir)
    choices = [root, root / "net_edge", root.parent / "net_edge"]
    choices += sorted(root.parent.glob("net_edge_20*"), reverse=True)
    for candidate in choices:
        if any((candidate / name).exists() for name in
               ("registry.json", "research/summary.json", "specialists/team_report.json", "team_report.json")):
            return candidate
    return None

def render_net_edge(artifacts_dir):
    root = find_net_edge_root(artifacts_dir)
    if root is None:
        return False
    st.subheader("ETF research and specialist team")
    st.caption("Advisory analysis. Frozen strategies and account risk rules retain order control.")
    summary = _read(root / "research" / "summary.json") or _read(root / "summary.json")
    registry = _read(root / "registry.json")
    if registry:
        st.write("Registered candidates: MR30, MR60, MOM20, MOM60")
        st.caption("SPY · QQQ · IWM · TLT · GLD")
    if summary:
        st.write("Research status", summary.get("mode", "historical_development").replace("_", " ").title())
        rows = summary.get("candidates", summary.get("results", []))
        if isinstance(rows, list) and rows:
            scalar_rows = [dict(candidate=r.get("candidate_id"), passed=r.get("development_passed"), net_pnl=r.get("metrics", {}).get("net_pnl"), business_pnl=r.get("metrics", {}).get("business_pnl"), trades=r.get("metrics", {}).get("trade_count"), max_drawdown=r.get("max_drawdown"), reasons=", ".join(r.get("reasons", []))) for r in rows]
            st.dataframe(pd.DataFrame(scalar_rows), hide_index=True, use_container_width=True)
        champion = summary.get("champion")
        if not champion:
            st.info("No candidate is qualified for prospective confirmation.")
        for reason in summary.get("blocking_reasons", []):
            st.write(str(reason))
    report = _read(root / "specialists" / "team_report.json") or _read(root / "team_report.json")
    if not report:
        st.caption("No specialist assessment has been captured yet.")
        return True
    sources = report.get("evidence_sources", {})
    names = {"technical": "Technical analyst", "fundamental": "Fundamental analyst",
             "news": "News analyst", "macro": "Macro analyst", "bear": "Bear / critic"}
    tabs = st.tabs(list(names.values()))
    by_role = {r["role"]: r for r in report.get("reports", [])}
    for tab, (role, name) in zip(tabs, names.items()):
        with tab:
            r = by_role.get(role)
            if r is None:
                st.write("Assessment unavailable.")
                continue
            st.write(r.get("stance", "insufficient_data").replace("_", " ").title())
            st.write(r.get("thesis", ""))
            st.caption("Confidence: " + r.get("confidence", "unknown") + " · " + r.get("status", "unknown"))
            for fact in r.get("facts", []):
                st.write(fact["text"])
                for source_id in fact.get("evidence_ids", []):
                    source = sources.get(source_id, {}) if isinstance(sources, dict) else {}
                    st.caption(source_id)
                    if source.get("source_url"):
                        st.link_button("View source", source["source_url"])
            if r.get("missing_data"):
                st.write("Missing evidence")
                for item in r["missing_data"]:
                    st.write("• " + item)
            if r.get("risks"):
                st.write("Risks and objections")
                for item in r["risks"]:
                    st.write("• " + item)
            if r.get("analysis_method") == "provider_interpretation_unverified":
                st.caption("Provider narrative is interpretation and has not been independently verified.")
    st.caption("Snapshot " + report.get("input_hash", "")[:16])
    return True
