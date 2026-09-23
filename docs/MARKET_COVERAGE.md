# Market coverage and Alpaca connection

The dashboard now has a **Browse markets** section that works even before research results exist. It searches a directory by symbol, instrument name, market type, and venue. The initial September 22, 2026 snapshot contains **21,436 venue-specific listings and contracts**. Counts are not unique underlying assets: an asset can trade on several venues, and every option strike/expiry is a separate contract.

| Public directory | Records downloaded | Actual scope |
|---|---:|---|
| Nasdaq-listed | 5,614 | U.S. securities on Nasdaq, excluding test issues |
| Other U.S. listed | 7,610 | Other U.S. exchange-listed securities, excluding test issues |
| Coinbase Exchange | 838 | That venue's published pairs, including disabled entries |
| Kraken | 1,450 | That venue's published pairs, including 12 fiat/fiat pairs |
| Deribit | 5,924 | That venue's instruments, including options, futures and perpetuals |

This does **not** provide every publicly tradable instrument worldwide. Non-U.S. equities, global bonds, OTC instruments, non-Deribit futures, and U.S. listed options need additional directory connections. Available public directories are current snapshots, not survivorship-free historical universes. Instrument names are used for some display classifications; provider IDs, venues, and contract details remain preserved.

Directory status is separate from historical-data availability and brokerage access. Every entry starts with unverified history and no connected execution. Listed or active instruments are not necessarily tradable by this user's account. The catalog does not add derivatives to the stock backtester, submit orders, or change the saved strategy's two-stock universe.

**Alpaca**

A read-only connection is implemented for Alpaca's paper API. It fetches active U.S. equity assets (including ETFs) and crypto assets. Optional U.S. options discovery follows pagination and uses an explicit expiry horizon. No account was connected during this work, so this adapter was validated with simulated API responses rather than a live authenticated request.

Alpaca documents stock, crypto, and options interfaces. It is not a complete worldwide market feed. This implementation does not assume that an Alpaca account provides forex, futures, or worldwide exchange access. Public Deribit and Kraken listings do not become Alpaca-tradable through this integration.

After creating an Alpaca paper account, configure `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` locally in the process environment. Do not paste keys into a chat, store them in this document, or commit them. The adapter only sends GET requests to two fixed directory endpoints on `paper-api.alpaca.markets`; it refuses redirects and never submits orders.

From the project directory:

```sh
.venv/bin/python scripts/discover_markets.py
.venv/bin/python scripts/discover_markets.py --alpaca
```

For options, add `--alpaca-options-through YYYY-MM-DD`, replacing the date with the last expiry you want included. The requested range and exclusions appear in the coverage report. Omitting this option does not silently accept Alpaca's short default expiry range. Repeated page tokens or a pagination limit produce a visible failure instead of a supposedly complete list.

Additional vendor directories can be imported using repeated `--import-file /path/to/directory.json` or CSV files. `--sources` selects public adapters; an empty `--sources` can be used with an import or Alpaca. Imports must follow the `Instrument` schema in `src/trader_engine/data/catalog.py`: provider, venue, symbol, name, kind, and timezone-aware observed_at are required. Dated derivatives also require expiry and contract size; options require strike and call/put. A nonempty import is validated in full before publication.

The default output is `artifacts/market_catalog/catalog.json`. The dashboard reads this directory next to its research artifacts. Failed sources are reported explicitly; zero successful results never overwrite an existing catalog. A mixed-success refresh publishes the successful current sources with failed sources visibly listed; it does not silently retain stale instruments from a failed source. Snapshots older than 24 hours show a warning. Directory requests occur only when the discovery command runs, not on each dashboard interaction.

**What remains before the requested 1,000 broad-market tests**

1. Connect the chosen historical-data feed and determine actual coverage, entitlements, rate limits, and history depth. Download in resumable batches with explicit missing-symbol/data reports. The two-stock September 22 data refresh is complete, but the expanded-universe 1,000 simulations have **not** run.
2. Build dated universe membership including delisted securities; today's listing directory alone would bias a historical stock test toward survivors. Use explicit tradability, liquidity and history rules without choosing symbols for favorable past returns.
3. Repair the existing state-quality identity and entry/exit selection defects. Add derivative-specific pricing, contract multiplier, expiry, exercise/assignment, settlement, margin, borrow/funding, currency conversion and calendar logic before claiming options/futures/forex profitability.
4. Freeze that evaluation protocol, run chronological tests, and then 1,000 explicitly labeled resampled scenarios with costs. Repeating one deterministic backtest 1,000 times does not add evidence.

Sources: [Nasdaq directory definitions](https://www.nasdaqtrader.com/Trader.aspx?id=SymbolDirDefs), [Coinbase products](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-all-known-trading-pairs), [Kraken pairs](https://docs.kraken.com/api-reference/market-data/get-tradable-asset-pairs), [Deribit instruments](https://docs.deribit.com/api-reference/market-data/public-get_instruments), [Alpaca assets](https://docs.alpaca.markets/us/docs/working-with-assets), [Alpaca options contracts](https://docs.alpaca.markets/us/reference/get-options-contracts).
