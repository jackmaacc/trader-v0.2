# Current-strategy YTD campaign — September 24, 2026

Completed **100,000 unique chronological execution-sensitivity trials**, each starting flat with $100,000. The scope was 50,000 H1 monthly-trend ETF trials and 50,000 BTC/ETH SMA200 research-proxy trials. Equity history runs through September 23 (182 sessions); crypto uses 266 completed UTC days through September 23, with fresh Alpaca daily data.

| Strategy | Worst net P&L | Median net P&L | Best net P&L | Median passive 25% basket |
|---|---:|---:|---:|---:|
| H1 ETFs | −$4.89 | $91.37 | $170.84 | $1,936.67 |
| BTC/ETH SMA200 | $3,564.67 | $4,262.84 | $4,967.91 | −$2,124.56 |

These are net trading results after modeled friction, before business overhead. Each row summarizes independent simulated accounts, not allocations within one combined portfolio. Passive benchmarks have different realized exposure and are not equal-risk alpha comparisons.

H1 still falls well short of its passive return comparison. Crypto is the more promising result here, but its apparent two completed trades are BTC and ETH holdings closed by the simulator's terminal liquidation. There are no demonstrated signal-driven exit cycles in the baseline; this is one overlapping rally, not repeated proof of an edge. At the fixed 25 bps fee/5 bps impact anchor, an extra day of signal delay lowers crypto net P&L from $4,945.38 to $3,708.47.

The campaign retains 22.4 million candidate daily records, 22.4 million matched-control daily records, all fills and exact scenario identities. All 27 full-history reference checks passed. Thirty focused new-kernel tests and 24 existing daily-engine tests passed. Every batch verifies finite values, cash, equity and fixed-fill gross-minus-cost identities. Results and reference controls do not enable trading.

Full evidence is in `artifacts/current_ytd_100000_20260924/`: `REPORT.md`, `protocol.json`, all-results CSV/Parquet, daily/fill batches, source/input archives, reference checks, validation and interactive cost/baseline charts. Large evidence stays outside Git. The frozen research intent is [YTD_100000_PROTOCOL.md](YTD_100000_PROTOCOL.md).

All YTD history is retrospective. The scenario grid varies costs and delays while retaining fixed signals and risk rules; its profitable fraction is not a probability of future profit. Crypto remains a research proxy for the paper manager, omitting its IOC/quote checks, fixed entry cap and broker lifecycle details. ETF corporate-action/data-provenance limitations remain disclosed in the report. No 1%-per-day claim, live qualification or strategy promotion follows from this run.

Independent completed-evidence review passed: all 44.8 million daily accounting records, 100,000 scenario/outcome identities and 950,000 candidate fills verified. Four non-anchor trials matched the reference engine and separate reconstruction, with maximum accounting discrepancy below $1.46e-11. All 100,000 crypto exit fills were terminal liquidations; none were signal-driven exits.
