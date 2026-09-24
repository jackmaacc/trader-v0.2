# Explicit stock market data configuration

The paper breakout and portfolio runners default to `iex`. The reusable
`StockFeedConfig` accepts only `iex` or `sip`; the code does not read an environment
feed override, probe subscriptions, switch subscriptions, or fall back to another
feed after a request fails. Selecting a feed does not grant access to it.

Both runners accept `--feed iex|sip`. Previewing either runner without
`--execute-paper` reports the selected feed and coverage without opening market
or broker connections. The Python interfaces are `run(..., feed="iex")`,
`latest_quotes(feed="iex")`, `latest_bars(feed="iex")`, and
`Market(feed="iex")`. Existing calls retain IEX behavior. A future SIP selection
must be deliberate and separately entitled; this change neither buys access nor
authorizes starting a runner. Leave the default at IEX for the current free plan.

The selection follows quote, minute-bar, and portfolio snapshot requests;
entry-retrieval, entry, signal, submission, and holding quote checks use the same
feed. Entry freshness remains 5 seconds and maximum spread remains 10 basis
points; hold policy, signals, symbol selection, risk and sizing limits are
unchanged. Entitlement/rate-limit errors are surfaced with their retrieval status
and selected feed; there is no retry against IEX when SIP fails.

`protocol.json`, `status.json` and previews record `feed`, `expected_feed`,
`market_data_coverage`, `restricted_market_data`, and `feed_fallback: false`.
IEX coverage is explicitly `single_exchange_iex` and restricted; SIP is
`consolidated_sip`. These describe the requested source, not a verified
subscription, successful response, or proof of complete data. Quote envelopes
record the selected request feed and quote policies record the expected feed.
Snapshot quote wrappers retain their supplied actual feed so mismatches fail.

This configuration applies to request feeds and diagnostic quote validation. It
does not relabel historical archives, alter frozen research registries, or require
future daily research to use IEX. Preserve each archive's actual feed provenance;
completed historical SIP data may be used when available under the applicable
access terms, independently of the live paper default.

Diagnostic shadow APIs keep their original SIP requirement:
`run_shadow(..., expected_feed="sip")` and
`run_shadow_snapshot(snapshot, registry_path, candidate_id, output,
expected_feed="sip")`. A caller may explicitly select `expected_feed="iex"`
for restricted local diagnostics. The existing `scripts/run_net_edge.py shadow`
CLI exposes the same choice as `--expected-feed iex|sip` and defaults to SIP.
Every decision, manifest, workflow input record
and result then carries the restricted IEX label. The selection is an API
argument, never inferred from a snapshot or observed quote. This remains
`diagnostic_shadow`, with no order authority and no formal forward-run approval.
An IEX diagnostic does not establish SIP-quality execution or profitability.

Validation is entirely offline: mocked HTTP requests cover both feeds, invalid
selection, quote gates, rejection evidence, and failures without fallback;
synthetic shadow inputs cover default SIP rejection and restricted IEX opt-in.
No trading runner, broker session, credential read, or subscription action is
required to test these changes.
