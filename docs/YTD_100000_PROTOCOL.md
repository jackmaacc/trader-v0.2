# September 24 current-model YTD campaign

## Frozen research intent

The user requested 100,000 YTD simulations and selected current daily strategies and the BTC/ETH paper strategy. The campaign covers the retained H1 monthly trend hypothesis and CRYPTO_SMA200 research baseline: 50,000 unique execution configurations each. Other hypotheses, rejected intraday MR/MOM candidates, options, futures and advisory/news agents are outside this campaign. No signal optimization, strategy selection, broker actions or changes to the running service are authorized by the simulations.

Each replay starts flat with $100,000 and retains its complete chronological YTD history. ETFs use January 2–September 23, 2026 (182 exchange sessions); crypto uses January 1–September 23 (266 completed UTC days). Earlier bars provide warmup only. These dates have been examined before. There is no untouched 2026 holdout, and no count of simulations converts this history into prospective evidence. Dates after the frozen endpoint remain outside the campaign.

H1 uses SPY, QQQ, IWM, TLT and GLD with the existing shared daily adapter's signals, position sizing, stops, portfolio halts, receivables and terminal marks. The experiment varies one-way execution costs across 1–28 bps and additional decision delay of zero or one session, with 25,000 costs per delay. The shared adapter begins without pre-start pending orders; a monthly strategy therefore need not enter on the first January session.

Crypto uses BTC/USD and ETH/USD, completed-close SMA200 signals, the existing baseline's 12.5% per-position and 25% aggregate entry limits, 3% daily entry halt and terminal liquidation convention. It varies impact across 1–30 bps and zero/one additional day of signal observation delay, retaining a modeled flat 25 bps fee per side. Fee tiers are not calibrated; this is an assumption. The current paper service's $12,400 hard entry cap, quote-age/spread/IOC gates, same-day retry restrictions, asset-denominated fee deductions and continuing terminal holdings are not faithfully reproduced by that baseline. Its results must be called a strategy research proxy, not an exact replay of broker execution.

Seed: 20260924. Signal rules and risk settings stay fixed throughout the campaign. The machine-readable protocol and scenario table must be written before the definitive campaign reference controls and main batch and must identify exact cost values, scenario identities, source/data hashes and benchmark conventions. Engineering parity checks and runtime probes on historical data occurred while implementing the accelerators, after this written research intent but before the final machine-readable freeze. They were used to identify implementation differences, not to select cost ranges, signals or risk rules. This is not a claim that every historical outcome was unseen before preregistration. Any implementation defect requiring correction must be documented and refrozen; preserve earlier evidence rather than silently overwriting it.

## Data

ETF inputs are the hash-verified `daily_hypotheses_20260923/corrected_inputs` archive. Raw execution prices and supplied exchange sessions are preserved. The SPY March 2026 action/adjustment discrepancy and unknown historical data arrival/revision times remain limitations. The 90 unknown payment dates in 2016–2019 and provisional QQQ 2022 correction are archive provenance issues; they are not 90 unpaid 2026 distributions for a flat-start YTD portfolio.

Crypto inputs were retrieved with the read-only Alpaca research `get_crypto_bars` connection, 1Day, from 2025-01-01T00:00:00Z to 2026-09-23T23:59:59Z. The response contains 631 consecutive bars per asset and no next page token. Positive finite consistent OHLCV and exact UTC-day coverage were checked. Raw response and transformed file hashes are retained. A separate public Yahoo refresh ended September 22 and is excluded from the campaign. Historical Alpaca daily bars do not establish the availability of executable quotes at a simulated fill.

## Validation and evidence

Any accelerated path must agree with the shared reference engine on representative complete histories and meaningful synthetic accounting/risk edge cases before the 100,000-run batch. Preserve per-scenario daily accounting and fills, including losses, inactivity and post-halt days. Verify cash plus marked positions plus receivables equals equity, and fixed-fill gross P&L minus modeled costs equals net trading P&L. Count unique configurations separately from unique outcomes.

Use matched-date passive benchmarks with costs and their allocation/terminal conventions stated. H1's initial 25% and 100% buy-and-hold baskets have different realized exposure from H1; they are not an equal-volatility alpha test. Crypto's passive comparison uses the existing shared baseline sizing and execution timing. Reference/parity/benchmark controls do not count toward the 100,000 requested candidate trials.

Batch ledgers into compressed files; bound memory/workers and check disk headroom throughout. Preserve incomplete output on failures. Independently audit the frozen implementation and completed results. Report gross, costs, net and unknown business overhead separately. The profitable fraction of this chosen grid is not a future-profit probability. No candidate is automatically promoted.
