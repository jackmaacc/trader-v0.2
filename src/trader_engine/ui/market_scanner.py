"""Read-only, freshness-explicit display of the continuous scanner's artifacts."""
from datetime import datetime, timezone
import json
from pathlib import Path

import pandas as pd
import streamlit as st

STALE_SECONDS = 600


def _read(path):
    try:
        payload = json.loads(path.read_text())
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError):
        return None


def _age(stamp, now):
    try:
        value = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if value.tzinfo is None:
            return None
        seconds = (now-value).total_seconds()
        return seconds if seconds >= 0 else None
    except (ValueError, TypeError):
        return None


def scanner_view(root: Path, now=None):
    """Load a defensive snapshot; a recent file alone never proves healthy data."""
    now = now or datetime.now(timezone.utc)
    status = _read(root / "status.json")
    latest = _read(root / "latest.json")
    instruments = _read(root / "instruments.json")
    age = _age((status or {}).get("checked_at"), now)
    if status is None:
        health = "Unavailable"
    elif age is None or age > STALE_SECONDS:
        health = "Stale"
    elif status.get("status") == "shutdown":
        health = "Stopped"
    elif status.get("status") == "scanning":
        health = "Scanning"
    elif status.get("status") == "healthy" and latest is not None:
        latest_age = _age(latest.get("checked_at"), now)
        health = "Healthy" if latest_age is not None and latest_age <= STALE_SECONDS else "Degraded"
    else:
        health = "Degraded"
    sources = (status or {}).get("sources", {})
    if not isinstance(sources, dict):
        sources = {}
    discoveries = (instruments or {}).get("last_success_at")
    catalog_ages = [_age(discoveries.get(kind), now) for kind in ("us_equity", "crypto")] if isinstance(discoveries, dict) else [_age(discoveries, now)]
    catalog_stale = not instruments or instruments.get("stale") is not False or any(age is None or age > 86400 for age in catalog_ages)
    if health == "Healthy" and (not sources or catalog_stale):
        health = "Degraded"
    if health == "Healthy" and ((status or {}).get("errors") or any(isinstance(s, dict) and (s.get("error") or s.get("status") in ("failed", "error", "degraded", "unavailable")) for s in sources.values())):
        health = "Degraded"
    raw = (latest or {}).get("records", [])
    rows = []
    if isinstance(raw, list):
        for record in raw:
            if not isinstance(record, dict) or not record.get("symbol"):
                continue
            row = dict(record)
            age_seconds = _age(record.get("data_asof"), now)
            row["data_age_minutes"] = round(age_seconds/60, 1) if age_seconds is not None else None
            row["freshness"] = "Unknown timestamp" if age_seconds is None else "Stale (>10 min)" if age_seconds > STALE_SECONDS else "Recent timestamp"
            row["state"] = record.get("state", "unknown")
            # A recent heartbeat must not turn old or closed-session observations live.
            if health in ("Stale", "Stopped", "Unavailable"):
                row["freshness"] = "Scanner " + health.lower() + "; saved observation"
            rows.append(row)
    if health == "Healthy" and not rows:
        health = "Degraded"
    return {"health": health, "age": age, "status": status or {}, "latest": latest or {}, "instruments": instruments or {}, "sources": sources, "rows": rows, "catalog_stale": catalog_stale}


@st.fragment(run_every="30s")
def render_market_scanner(root: Path):
    """Render the scanner artifact directory; does not fetch prices or trade."""
    view = scanner_view(Path(root))
    st.subheader("Continuous market scanner")
    st.caption("Read-only monitoring • refreshes every 30 seconds while this dashboard is open. Price movement is an observation, not a proven trading edge.")
    health = view["health"]
    message = f"Scanner: {health}"
    if health in ("Unavailable", "Stale", "Stopped", "Degraded"):
        st.warning(message + ". Coverage and observations may be incomplete or out of date.")
    else:
        st.info(message + ". Check each source and observation timestamp below.")
    status = view["status"]
    st.caption(f"Last heartbeat: {status.get('checked_at', 'not recorded')} · Last completed cycle: {status.get('cycle_completed_at', 'not recorded')} · Snapshot: {view['latest'].get('checked_at', 'not recorded')}")
    progress = status.get("progress", {})
    if not isinstance(progress, dict):
        progress = {}
    st.write(f"Stage: {status.get('stage', status.get('status', 'not started'))} · Batch progress: {progress.get('processed', '?')} / {progress.get('total', '?')}")
    coverage = status.get("coverage", {})
    if not isinstance(coverage, dict):
        coverage = {}
    st.write(f"Reported coverage: {coverage.get('total', 'unknown')} instruments · Displayable observations: {len(view['rows'])}")
    by_state = coverage.get("by_state", {})
    if isinstance(by_state, dict) and by_state:
        st.caption("Coverage states: " + "; ".join(f"{key}: {value}" for key, value in by_state.items()))
    catalog = view["instruments"]
    if not catalog:
        st.warning("Instrument catalog is missing or unreadable; complete universe coverage is unverified.")
    elif view["catalog_stale"]:
        st.warning("Instrument catalog is older than 24 hours, marked stale, or its freshness cannot be verified.")
    st.caption(f"Universe last successful discovery: {catalog.get('last_success_at', 'not recorded')}. Scope: Alpaca-listed U.S. stocks/ETFs and USD crypto pairs, plus indicative futures proxies. Index membership is not verified.")
    equities = view["sources"].get("equities", {})
    feed = equities.get("feed") if isinstance(equities, dict) else None
    feed_text = {"sip": "Equities request SIP consolidated data; availability is shown in source status below.", "iex": "Equities request IEX-only data, not consolidated market quotes; availability is shown in source status below."}.get(feed, "Equity feed is not verified in the saved source status.")
    st.caption(feed_text + " Each observation retains its actual source and feed; requested feed does not relabel older observations. Futures are indicative continuous-contract proxies and are not eligible for execution here. Closed sessions, delayed feeds and stale prices do not represent live opportunities.")
    source_rows = []
    for name, source in view["sources"].items():
        if not isinstance(source, dict):
            continue
        source_rows.append({"Source": name, "Status": source.get("status", "unknown"), "Feed": source.get("feed", "not reported"), "Market open": source.get("market_open", "not reported"), "Error": str(source.get("error") or "")})
    if source_rows:
        st.dataframe(pd.DataFrame(source_rows).astype(str), hide_index=True, width="stretch")
    else:
        st.warning("No source status is available.")
    errors = status.get("errors", [])
    if isinstance(errors, list):
        for error in errors:
            st.warning(str(error))
    if not view["rows"]:
        st.info("No instrument observations have been saved yet. The scanner may be starting, unavailable, or waiting for its first completed cycle.")
        return
    frame = pd.DataFrame(view["rows"])
    search = st.text_input("Search symbols or sources", key="continuous_scanner_search")
    classes = sorted(frame.get("asset_class", pd.Series(dtype=str)).dropna().astype(str).unique())
    selected = st.multiselect("Asset classes", classes, default=classes, key="continuous_scanner_classes")
    if "asset_class" in frame:
        frame = frame[frame["asset_class"].isin(selected)]
    if search:
        frame = frame[frame.astype(str).apply(lambda col: col.str.contains(search, case=False, regex=False)).any(axis=1)]
    if "change_pct" in frame:
        frame["change_pct"] = pd.to_numeric(frame["change_pct"], errors="coerce")
        frame = frame.loc[frame["change_pct"].abs().sort_values(ascending=False, na_position="last").index]
    columns = [c for c in ("symbol", "asset_class", "source", "feed", "requested_source", "requested_feed", "state", "freshness", "price", "change_pct", "volume", "spread_bps", "data_asof", "comparison_asof", "volume_asof", "data_age_minutes", "observation_count", "last_seen") if c in frame]
    st.caption(f"{len(frame)} matching observations. Sorted by absolute price change; click column headings to sort. Compare changes only when their source intervals match.")
    st.dataframe(frame[columns], hide_index=True, width="stretch")
