# Chronological YTD execution sensitivity

10,000 complete chronological scenarios: 2,500 each for MR30, MR60, MOM20 and MOM60 on SPY, QQQ, IWM, TLT and GLD. Each retains all 182 completed 2026 sessions and starts flat with $100,000. Pre-2026 daily history warms up signals. Shared Python signals feed a C++17 continuous ledger; 24 full histories must match the Python reference before the batch starts. This is not a bootstrap.

## Reproduction

Use the project virtual environment and clang++ (C++17). Run:

    .venv/bin/python scripts/run_etf_ytd_10000.py --inputs artifacts/ytd_execution_10000_20260923/inputs --output artifacts/ytd_execution_10000_reproduction --workers 6 --reference-workers 4

Output must not exist. Reserve at least 500 MB free for intermediate arrays, input copies and results. The archived source snapshot and protocol hashes are authoritative if the checkout changes.

The fixed grid varies one-way modeled impact from 1–28 bps and execution delay from 0–3 minutes. Spread and commission are zero to avoid double counting. Assumptions are not calibrated live costs. Delay affects entries and scheduled momentum exits; stops, targets and deadlines retain minute-bar semantics.

## Corrections and remaining gates

Research execution now clamps an already-marketable initial stop to the observed open and uses that executable price in initial risk sizing. The previous nominal-stop assumption could create favorable unrealistic fills. September SPY/QQQ distributions omitted by an earlier processing-date query are included by elapsed ex-date; future payments remain receivables.

Five provider-confirmed missing TLT minutes remain gaps. Corporate-action publication times and precise dividend cash credit times remain unverified. Recurring overhead is unknown, so net trading profit does not establish business profit.

The accelerated and reference research engines clamp both initial and aggregate marketable-stop risk to executable prices. Diagnostic shadow and AccountRisk now apply the same conservative principle; quote-based shadow uses the executable bid and carries the resulting exit cost into later proposals. Offline regression coverage includes simultaneous entries under a tightened portfolio cap. Existing saved campaigns predate the aggregate-risk correction; do not silently relabel them as runs of the current code. These targeted checks do not establish full replay/shadow parity or broker readiness.

All trials share one historical path. The profitable fraction describes the cost grid, not the probability of future profit. These are development results, with no champion selection, prospective confirmation, purchase or broker orders.
