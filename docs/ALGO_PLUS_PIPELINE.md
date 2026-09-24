# Algo Trader Plus data and decision pipeline

This deployment adds continuously supervised data and diagnostic services to the existing scanner. It does **not** broaden the paper account's order mandate: `paper_crypto_service.py` remains the sole broker writer, running the existing BTC/ETH daily SMA200 experiment. Equity and option diagnostics cannot submit orders. No prior failed strategy has been promoted or tuned.

## Capabilities and deployment

| Capability | Implementation | Deployed scope |
| --- | --- | --- |
| Consolidated stock snapshots | Existing SIP scanner | Full active Alpaca equity directory |
| Unlimited stock stream symbols | SIP WebSocket wildcard minute bars | All symbols the provider delivers; bounded 25,000-symbol cache |
| Consolidated streaming quotes | Same SIP connection | 20 liquid ETFs |
| Historical data without the latest-15-minute restriction | Paginated SIP bar client | Latest 30 minutes for 10 core ETFs plus up to 20 fresh scanner selections |
| Higher historical API allowance | Paced paginated client, bounded retries | At most five client requests/second; no attempt to saturate the account limit |
| OPRA consolidated options snapshots | Paginated chain intake and contract metadata | SPY, QQQ, IWM, expirations from today through 45 days |
| OPRA streaming quotes | Separate msgpack WebSocket connection | 100 active unexpired contracts, balanced across the three underlyings and ordered near the latest quoted underlying price; configurable up to 1,000 |
| Decision evaluation | Existing frozen breakout detector, unchanged | Diagnostic only; every result includes data or strategy rejection reasons |

The 100 streaming options are a deterministic collection sample balanced across three underlyings, ordered by distance from the latest underlying quote. This is not a ranking of expected profit. The quality screen assesses the first 100 sorted snapshots per underlying and reports the returned population and truncation. It checks quotes, Greeks, IV and contract metadata; passing those checks does not establish expected returns. UI labels expire with quote age. The streaming selection and assessed sample are not claimed to match or represent the full chain.

The REST client exposes explicit history start/end, timeframe and adjustment. It preserves feed, request parameters, pagination completeness and missing-symbol lists. Pagination completion is not complete market coverage. Historical revisions and current universe selection are not point-in-time backtest guarantees. Recent stock bars may legitimately be absent outside trading hours. No backtest or new performance claim is produced by this service.

## Service boundaries

- `com.trader-v02.plus-sip`: `scripts/plus_stream_service.py --feed sip --directory artifacts/plus_stream`.
- `com.trader-v02.plus-opra`: the same script with `--feed opra --directory artifacts/plus_options_stream --options-file artifacts/plus_research/options.json --max-option-quotes 100`.
- `com.trader-v02.plus-research`: `scripts/plus_research_service.py --directory artifacts/plus_research`.

Each worker has a separate account-derived lock, fixed data origins, sanitized error output and a `SHUTDOWN` marker in its artifact directory. The stream connects once per endpoint, verifies authentication and exact subscriptions, and reconnects with bounded backoff. A connected stream outside market hours is not reported as fresh data. Retained quotes from a previous connection cannot replace current REST evidence. No IEX or indicative-options fallback occurs.

The research worker waits 60 seconds after each full cycle. Options refresh at least five minutes apart and may lengthen a cycle; this is not a hard per-minute latency guarantee. SIP stream quotes replace REST quotes only when newer, fresh and validated as belonging to the current stream session. The underlying signal uses completed contiguous minute bars. Daily strategy H1 and rejected MR/MOM families are not rerun.

Snapshots overwrite bounded files rather than retaining a raw tick archive. A 1 GiB free-space guard stops additional large writes. These operational snapshots are not a complete prospective performance journal. `launchd` supervises crashes; intentional clean shutdown stays stopped. The Mac must remain on, logged in and online. Data can stop despite a running process, which is visible through timestamps and source errors.

## Verified access

On September 24, 2026 UTC, read-only tests authenticated and confirmed SIP wildcard bars with 20 ETF quotes and OPRA with 100 contract quotes. Both bounded test connections closed before service deployment. REST intake returned 4,668 SPY, 4,904 QQQ and 2,697 IWM option snapshots with exhausted pagination, plus 12,270 contract metadata records. These counts are one observation, not a coverage promise. Ten SIP quotes returned; the requested overnight recent-bar window had no bars, which produced data-blocked decisions.

## Remaining execution work

There is no approved equity or option strategy in the current research registry. The new diagnostic layer is running software, not completion of a profitable all-market trading engine. Equity paper execution needs a frozen experiment, explicit sizing/exit policy, prospective evidence and a reviewed integration into the existing account owner. The current generic equity lifecycle and reservations can be reused, but its risk policy differs from the crypto worker and cannot silently replace it. Options additionally require expiry, exercise, assignment, multiplier, permissions and buying-power handling. Futures remain indicative external proxies without an execution adapter. News and fundamental data are separate capabilities, not benefits verified by this subscription upgrade.

Official entitlement reference: https://docs.alpaca.markets/us/docs/about-market-data-api

## Release checks

The integrated focused regression run passed 136 tests covering Plus REST/streams/decisions/service, scanner/UI, and existing crypto manager/service/UI. Independent review passed 59 Plus-specific tests, including native OPRA MessagePack timestamps and retained-stream rejection. The dashboard rendered the actual ten decisions and 300 option assessments without exceptions.

After deployment, both streams reported authenticated subscriptions with no errors and `subscribed_idle`/`awaiting_data` outside market hours. The evaluator completed its cycle without API errors, and the existing crypto worker remained running. Live in-session message throughput and reconnect behavior under real load remain to be observed; offline tests do not replace that measurement. The existing read-only watchdog now covers these three services too.
