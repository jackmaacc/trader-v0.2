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

Complete broker cash-flow/trade accounting, immutable historical research archives, instrument/account-permission audits, overnight BOATS integration, unified portfolio execution, PC startup/remote-access verification and investment qualification are still outstanding. One month of observing these processes cannot claim completion of those workstreams.
