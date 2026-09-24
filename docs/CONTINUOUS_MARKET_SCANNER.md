# Continuous market scanner

The scanner is a read-only observation service with no end date. It polls the available universe with a 60-second pause after each completed scan (the scan duration adds to that interval), preserves current instrument records across restarts, and keeps scanning when no instrument presents an actionable observation. It cannot place orders and does not change the separate BTC/ETH paper worker's trading policy.

## Coverage and meaning

- All active, Alpaca-tradable US equity-class instruments returned by the asset directory, including ETFs. This broader listing universe is not independently verified S&P 500 or Nasdaq-100 membership. Index membership and historical constituent changes are not inferred from it.
- Active Alpaca crypto pairs quoted in USD, subject to the provider returning usable snapshots.
- Eight public continuous-futures price proxies: ES, NQ, YM, RTY, GC, SI, CL and ZN. These are indicative research observations, not contract-specific executable quotes. Provider delay and contract-roll effects are not calibrated; no futures orders are enabled.

The deployed equity scanner explicitly selects consolidated SIP snapshots using the Algo Trader Plus entitlement. SIP REST access and WebSocket authentication/subscription were verified. This worker uses periodic HTTP polling; the verification connection is closed and is not a persistent tick stream. There is no silent fallback to IEX on entitlement or source failures. The portable CLI retains IEX as its default, with a 300-second minimum pause; SIP permits 60 seconds and spaces data requests by at least 0.2 seconds. Missing observations remain possible. Crypto uses the Alpaca US venue. Futures proxies use public Yahoo chart data. Each source retains its identity and data timestamp; fetching an old observation does not make it current. Missing, malformed, stale and unavailable data must remain visible.

The scanner produces descriptive observations such as price changes, reported volume, spread where valid, and data freshness. A large move is not an expected profit, validated signal, or instruction to buy. It does not run hundreds of independent language models, obtain fundamentals/news that have no configured source, or automatically retune strategies. Its instrument records are observational history, not proof of learned predictive skill.

## Operation

The service has its own process lock and GET-only connections. It does not obtain the paper order writer's lifetime lock, submit orders, cancel orders or modify broker configuration. Credentials are obtained ephemerally from the already configured paper Keychain entry and are restricted to approved Alpaca origins; they must never be sent to the public futures endpoint or persisted in outputs.

Local artifacts are stored in `artifacts/continuous_market_scan/`: the latest scan, instrument records, coverage/health status, and bounded daily snapshots. Seven daily files retain the last complete scan for each day, rather than every cycle. Active instrument counters survive restarts; removed instruments remain for seven days before pruning. This is not a tick archive. Disk headroom checks prevent the scanner from consuming the space required by other services.

macOS LaunchAgent `com.trader-v02.market-scanner` supervises the worker and restarts it after crashes. It runs independently of this chat, within the user's local session. The launcher prevents idle system sleep while connected to AC power. The Mac must remain on and online; closing the lid, logging out, power loss or lost connectivity can interrupt scans. There is no cloud uptime guarantee.

The dashboard displays scan progress, coverage, source failures, timestamps and a searchable instrument table. A separate read-only watchdog reports meaningful failures or stale service health. Neither the UI nor watchdog can submit trades. The subscription does not supply a new futures execution connection or establish a profitable strategy. Existing paper-position management remains a separate service.

## Deployment and validation (September 23, 2026, Eastern)

The installed LaunchAgent explicitly runs `scripts/market_scanner_service.py --directory artifacts/continuous_market_scan --feed sip --poll-seconds 60`. A `SHUTDOWN` file in that directory ends the scanner cleanly; it does not close or alter paper positions. Remove that marker before manually restarting the scanner. The launcher is local deployment state, not a portable committed machine path.

SIP REST snapshots and a short SIP WebSocket authentication/bar-subscription probe succeeded. The probe disconnected afterward. A full read-only pass requested 13,495 equity listings, 36 USD crypto pairs and eight futures proxies. It received 13,190 equity snapshots, all 36 crypto snapshots and all eight proxies; missing, invalid and stale observations remained explicit. Two transient access errors recovered through bounded retries. No inference about profitable opportunities follows from those counts. This pass took about 35 seconds; the next cycle starts after the additional 60-second pause.

HTTP 403 recovery uses a same-feed retry, then batch subdivision if necessary, with a shared cap of 32 extra requests per cycle. Unresolved symbols retain errors and never fall back to another feed. The scanner does not retry indefinitely or interpret access failure as evidence about an instrument's market type.

Validation: 115 focused tests passed across scanner, dashboard, market-feed configuration, market catalog and existing crypto management. Independent scanner/UI review passed 40 tests. A Streamlit AppTest rendered all 13,539 actual instrument records with no exceptions. Existing paper-worker status remained running with no pending order. Raw market data and credentials are excluded from the commit.

The subscription's options data and higher streaming limits are not yet integrated into this worker. Futures remain public indicative proxies. Strategy changes and wider execution require separate validation; this deployment improves observation coverage and persistence.


## Empty futures window recovery

On September 24 at midnight Eastern, public futures charts returned a valid empty `1d` window. The scanner now makes one `5d` request only for that recognized empty-window shape. Malformed payloads and HTTP failures do not trigger the fallback. Returned prices retain their actual timestamps and indicative-only classification; no metadata price is substituted for a missing bar. If both windows are empty, the source remains unavailable. The deployed read-only scanner subsequently received all eight proxies again. No paper execution service was restarted for this fix.
