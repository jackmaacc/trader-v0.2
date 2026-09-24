"""Read-only views for frozen research and the five advisory specialists."""
from pathlib import Path
import json
import streamlit as st
import pandas as pd

SPECIALISTS = {
    "technical": "Technical analyst", "fundamental": "Fundamental analyst",
    "news": "News analyst", "macro": "Macro analyst", "bear": "Bear / critic",
}
EVIDENCE_NEEDED = {
    "technical": "Needs dated price bars, their interval and source; VWAP is optional.",
    "fundamental": "Needs sourced ETF holdings, NAV and expense ratios with effective dates.",
    "news": "Needs cited headlines with publication and availability timestamps.",
    "macro": "Needs released economic observations with release timestamps and data vintages.",
    "bear": "Needs the other four assessments to challenge their evidence and identify gaps.",
}


def _reports_by_role(report):
    return {r["role"]: r for r in report.get("reports", [])
            if isinstance(r, dict) and r.get("role") in SPECIALISTS}


def _source_map(report):
    sources = report.get("evidence_sources", {})
    if isinstance(sources, dict):
        return {key: value for key, value in sources.items() if isinstance(value, dict)}
    # TeamReport dataclass exports contain a list; workflow exports use a mapping.
    if isinstance(sources, list):
        return {s["source_id"]: s for s in sources
                if isinstance(s, dict) and isinstance(s.get("source_id"), str)}
    return {}


def specialist_coverage(report):
    """Describe saved evidence coverage without manufacturing an assessment."""
    by_role = _reports_by_role(report)
    rows = []
    for role, name in SPECIALISTS.items():
        r = by_role.get(role, {})
        facts = r.get("facts", [])
        if not r:
            status = "Assessment not captured"
        elif r.get("status") in ("failed", "timeout"):
            status = "Assessment " + r["status"]
        elif not facts:
            status = "No usable evidence"
        elif r.get("missing_data"):
            status = "Evidence incomplete"
        else:
            status = "Saved evidence available"
        rows.append({"Specialist": name, "Evidence status": status,
                     "Facts": len(facts), "Confidence": r.get("confidence", "unknown")})
    return rows

def _read(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None

def find_net_edge_root(artifacts_dir):
    root = Path(artifacts_dir)
    choices = [root, root / "net_edge", root.parent / "net_edge"]
    choices += sorted(root.glob("net_edge_20*"), reverse=True)
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
    report = report or {}
    st.subheader("Specialist assessments")
    st.write("Saved advisory snapshot — these panels do not run live analysis or place trades.")
    st.caption("Artifact folder: " + str(root))
    if not report:
        st.info("No specialist assessment has been captured for this artifact folder.")
    st.dataframe(pd.DataFrame(specialist_coverage(report)), hide_index=True, use_container_width=True)
    sources = _source_map(report)
    timestamps = sorted({str(s["available_at"]) for s in sources.values() if s.get("available_at")})
    if timestamps:
        st.write("Source availability timestamps: " + " through ".join(dict.fromkeys((timestamps[0], timestamps[-1]))))
        st.caption("These are saved source timestamps, not a live freshness check or the price-bar window.")
    elif report:
        st.warning("Source timestamps unavailable; freshness cannot be established.")
    tabs = st.tabs(list(SPECIALISTS.values()))
    by_role = _reports_by_role(report)
    for tab, (role, name) in zip(tabs, SPECIALISTS.items()):
        with tab:
            st.markdown("### " + name)
            r = by_role.get(role)
            if r is None:
                st.info("Assessment not captured in this saved snapshot.")
                st.write(EVIDENCE_NEEDED[role])
                continue
            st.write("Stance: " + r.get("stance", "insufficient_data").replace("_", " ").title())
            st.write(r.get("thesis") or "No assessment narrative was captured.")
            st.write("Confidence: " + r.get("confidence", "unknown") + " · Analysis status: " + r.get("status", "unknown"))
            facts = r.get("facts", [])
            if not facts:
                if r.get("status") in ("failed", "timeout"):
                    st.warning("The saved assessment " + r["status"] + "; no conclusion is available. Input coverage cannot be inferred from this failure.")
                else:
                    st.info("No usable evidence was supplied for this assessment. This is a data gap, not a neutral market signal.")
                    st.write(EVIDENCE_NEEDED[role])
            else:
                st.markdown("**Evidence in the saved assessment**")
            for fact in facts:
                st.write(fact["text"])
                for source_id in fact.get("evidence_ids", []):
                    source = sources.get(source_id, {})
                    st.caption("Source: " + source_id)
                    if source.get("available_at"):
                        st.caption("Available: " + str(source["available_at"]))
                    if source.get("source_url"):
                        st.link_button("View source", source["source_url"])
            if r.get("missing_data"):
                st.markdown("**Missing evidence**")
                for item in r["missing_data"]:
                    st.write("• " + item)
            if r.get("risks"):
                st.markdown("**Risks and objections**")
                for item in r["risks"]:
                    st.write("• " + item)
            if r.get("analysis_method") == "provider_interpretation_unverified":
                st.warning("Provider narrative is interpretation and has not been independently verified.")
    if report:
        st.caption("Snapshot " + report.get("input_hash", "")[:16])
    return True
