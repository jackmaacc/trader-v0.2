# Component inventory — September 24, 2026

This is a deployment baseline, not an investment approval. Actual health comes from timestamped service status, not this document. All local state below is beneath `artifacts/` and excluded from Git.

| Component / current Mac label | Authority | Persistent output | Stop/control boundary |
| --- | --- | --- | --- |
| `paper_crypto_service.py` / `com.trader-v02.paper-crypto` | Sole paper order writer; existing BTC/ETH SMA200 experiment | `paper_crypto_service/` state, status, journal | STOP disables entries; SHUTDOWN stops management and leaves holdings; never start a competing owner |
| `market_scanner_service.py` / `com.trader-v02.market-scanner` | GET-only observations; no orders | `continuous_market_scan/` latest, catalog, daily snapshots | SHUTDOWN affects only scanning |
| `plus_stream_service.py --feed sip` / `com.trader-v02.plus-sip` | SIP data subscription only | `plus_stream/` latest/status | SHUTDOWN closes stream, not positions |
| `plus_stream_service.py --feed opra` / `com.trader-v02.plus-opra` | OPRA data subscription only | `plus_options_stream/` latest/status | SHUTDOWN closes stream, not positions |
| `plus_research_service.py` / `com.trader-v02.plus-research` | Data-quality and unchanged breakout diagnostics; execution always disabled | `plus_research/` inputs, decisions, options, status | SHUTDOWN affects diagnostics |
| `paper_trial_service.py` / `com.trader-v02.paper-trial` | Reads saved artifacts; no network, secrets or broker methods | `paper_trial/observations.sqlite3`, status | SHUTDOWN stops observer; missing observations interrupt qualification |
| Streamlit dashboard | Reads artifacts; no new order controls | No ownership state | Closing UI does not stop workers |
| `watch-paper-crypto-manager` heartbeat | Read-only health inspection and meaningful failure notices | Existing task | Never restarts services, places orders or changes strategy rules |

Runtime renderer labels for a future host are generated from its explicit manifest and may differ from the current Mac labels. They are inert files, not installed services. The paper observation template is disabled and cannot authorize orders. Never install an additional template beside an active equivalent worker merely because its label differs.

## Strategy authority

- **BTC/ETH daily SMA200:** authorized limited paper experiment, not demonstrated profitable strategy; current 12.5%-per-entry/25%-aggregate entry sizing and 3% entry-only daily halt remain unchanged.
- **Existing intraday breakout:** diagnostic only in the Plus pipeline. No automatic order promotion.
- **MR30/MR60/MOM20/MOM60:** retained failures; not reopened by the data upgrade.
- **H1–H4 daily studies:** none passed the original hurdle. H1 remains a separately proposed research lead.
- **Options:** data-quality/contract research only; exercise, assignment and portfolio lifecycle not enabled.
- **Futures:** public indicative continuous proxies only; no executable futures feed or order adapter.
- **Live account:** not configured or activated by this milestone.

## Known foundation gaps

Complete broker cash-flow/trade accounting, immutable historical research archives, richer instrument eligibility capture and periodic account-permission refresh, overnight BOATS integration, unified portfolio execution, PC startup/remote-access verification and investment qualification are still outstanding. One month of observing these processes cannot claim completion of those workstreams.

## Accountable owners and component coverage

Jack is the project operator and retains decisions on money, execution activation and host handoff. The coordinator integrates reviewed changes. The following are engineering responsibility assignments to the existing specialist roles, not additional running agents or delegated authority to place orders. Independent review is required before execution/risk changes.

| Component or family | Engineering owner | Purpose and activation boundary |
|---|---|---|
| Paper crypto service, execution journals and `execution/` | `execution_engineer`; risk review by `portfolio_risk` | Existing single paper writer only. Other adapters require a separately reviewed activation; no live authority. |
| Market scanner, `data/`, catalog discovery and acquisition/import scripts | `data_quality` | Observe, normalize and validate data; ingestion does not approve an instrument or create an order. |
| SIP/OPRA streams and Plus research worker | `data_quality`; service reliability by `reliability_engineer` | Data subscriptions and diagnostic decisions only; no equity/options execution. |
| Trial observer, `operations/`, backup/preflight/release tools and `deploy/` templates | `reliability_engineer` | Record health and prepare portable operations; templates/receipts/backups never activate an executor. |
| Capability audit CLI and saved permission evidence | `data_quality` | Separate observed listings/data/account state from implementation/strategy readiness; report has no trading authority. |
| Dashboard, `ui/` and `app.py` | `dashboard_engineer` | Display research and saved operational evidence; closing the UI does not stop workers. Capability panel is read-only. |
| Read-only watchdog automation | `reliability_engineer` | Report meaningful failures; no restart, broker mutation or parameter changes. |
| Five application advisers in `agents/` | `research_designer` | Technical, fundamental, news, macro and critic analysis remains advisory; it cannot override frozen execution/risk rules. |
| `research/`, `features/`, `states/`, `markov/`, `signals/`, `intraday/`, research workflows | `research_designer` | Develop/freeze hypotheses and produce diagnostic signals; no automatic promotion or order mandate. |
| `backtest/` including the C++ replay core, simulation/sweep/walk-forward/comparison scripts | `simulation_runner`; review by `backtest_auditor` | Reproduce frozen research with accounting evidence; simulated profitability cannot activate trading. |
| `analytics/`, execution evidence audits and accounting outputs | `trade_reconciler` | Reconcile records and expose missing evidence; not an order writer or investment approval. |
| `risk/` and policy configuration | `portfolio_risk` | Review constraints for the specific runner. Future pilot planning never silently replaces the existing experiment's policy. |
| `core/`, shared configuration, package wiring and `workflows/` orchestration | Coordinator; relevant domain specialist reviews | Shared contracts inherit the caller's scope. Generic workflow access is not execution authorization. |
| `tests/`, fixtures and regression checks | `test_engineer`; separate `independent_reviewer` | Offline correctness evidence; tests never authorize broker-connected execution. |
| Legacy `paper_execution.py`, `paper_until_close.py`, `paper_breakout_until_close.py`, `paper_portfolio_until_close.py` and other order-capable entry points | `execution_engineer` | Retained implementation, not an additional authorized writer. Do not launch beside the crypto owner; requires an explicit reviewed ownership handoff. |
| Historical artifacts and research archives | `data_quality` for provenance; `trade_reconciler` for accounting | Preserve evidence, including failed/interrupted studies. Prior artifacts and displayed returns are not present execution permission. |
| Roadmap, mandate and acceptance records | Coordinator; Jack retains approval decisions | Track phases, owners and gates; documentation changes do not grant live execution. |

## Phase 0 capability baseline

The read-only audit is implemented and independently reviewed. Its September 24 snapshot is under `artifacts/capability_audit_20260924/` and documents 13,495 equity-class instruments (including OTC), 12,270 sampled option contracts, 36 crypto instruments and eight indicative futures proxies. These are observed listings, not a universally executable account universe. The bound saved paper snapshot reports options approval/effective level 3; live permission and strategy approval are not inferred.

Instrument-level fractional, margin, shorting and session attributes remain incompletely retained. Those are explicit later-phase data/adapter requirements, not hidden assumptions in this baseline. See [MARKET_CAPABILITY_AUDIT.md](MARKET_CAPABILITY_AUDIT.md) and [PHASE0_ACCEPTANCE.md](PHASE0_ACCEPTANCE.md).
