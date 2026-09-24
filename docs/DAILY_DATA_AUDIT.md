# Daily hypothesis data audit — September 23, 2026

Offline archive inspection only. No strategy outcomes, broker calls, credentials or network access were used by this audit. The archive supports a useful research implementation, but does not provide qualifying point-in-time evidence.

## Exact inputs and findings

`scripts/prepare_daily_hypothesis_inputs.py` verifies the recorded SHA-256 hashes before constructing `artifacts/daily_hypotheses_20260923/inputs`. It consumes the original `artifacts/net_edge_20260923/raw` archive and refreshed action ledger from `artifacts/ytd_execution_10000_20260923/inputs/dataset/actions.parquet`.

- The original raw calendar contains **2,696 actual sessions, January 4, 2016–September 23, 2026**. Its dates exactly match raw and adjusted daily bars for all five ETFs. The old normalized calendar's 2024 start was an importer restriction, not an absence of earlier archived calendar data.
- All ten daily source hashes match the original manifest. All five raw OHLCV sets are finite, have positive prices/nonnegative volume, ordered unique timestamps and consistent OHLC ordering. No sessions were fabricated or dropped.
- The normalized daily OHLC columns are adjusted prices. They are **not executable raw prices**. Multiplying adjusted opens by the close-derived scale creates rounding errors up to $0.012364. The prepared bundle instead takes raw opens directly from raw response archives.
- The original corporate-action response contains 254 cash-dividend records, no pagination token and no other action categories. Its request spans the full 2016–2026 interval. The refreshed ledger supplies two additional September 2026 ex-dates, resulting in 256 records. The two newly recorded dividends are SPY September 18 and QQQ September 21, not two September 21 events.
- Ninety 2016–2019 action records lack payment dates: 22 in 2016, 24 in 2017, 23 in 2018 and 21 in 2019. They can accrue as receivables but cannot be assumed spendable cash. This restricts fidelity of the older cash ledger; no payment date is inferred.
- QQQ has **two separate source IDs on September 19, 2022**, both $0.51856, with payment dates September 23 and October 31. The archived adjusted-price discontinuity implies roughly $1.03469, consistent with two distributions. Neither summing nor deleting one is an independently verified repair from these archives alone. The original prepared bundle preserves both and records this ambiguity; a strict runner should block duplicate same-symbol ex-dates.
- SPY's March 20, 2026 adjusted factor implies about $1.82257, whereas its archived cash dividend is $1.796999. Adjusted-factor inference is a diagnostic, not an authoritative cash ledger. Sub-cent factor noise elsewhere can arise from rounded adjusted prices; it should not become invented distributions.
- Corporate-action publication timestamps, historical bar arrival times, revision history and measured execution costs remain unknown. Existing manifests already identify development-only history. Calendar/bar completeness does not remove these limits.

## Engine input contract

Each `daily/<symbol>.parquet` contains raw `open/high/low/close/volume`, `total_return_close`, adjusted `signal_high/signal_low/signal_close`, and `signal_scale`. Daily indexes are midnight America/New_York labels, not bar availability timestamps. `schedule.parquet` supplies the actual UTC open/close and previous-session date. `actions.parquet` uses ex-date market-open timestamps and separate documented payment timestamps; unknown payments remain NaT.

The final archived date, September 23, is Wednesday and not month-end. Do not invent a future calendar session or treat the last archive row as a weekly/monthly rebalance boundary. The builder preserves all original evidence and writes qualification_allowed=false with source hashes and blockers.

## Verification performed

Ran the preparation script against the immutable archives; it completed with 2,696 sessions per ETF, 256 action rows and 90 unknown payment dates. Inspected all daily values, calendar equality, source hashes, duplicate action identities/ex-dates and adjusted-factor discontinuities. No strategy was executed. The 2016–2023 development accounting ambiguity must be resolved explicitly or remain blocked; later 2024–2026 results are consumed diagnostics, never fresh validation.

## Explicit provisional diagnostic correction, fixed before outcomes

The coordinator authorized a separate `corrected_inputs` bundle and the explicit `--provisional-qqq-correction` builder flag. The original bundle remains unchanged. The default builder preserves the ambiguous records.

The provisional path removes only QQQ source ID `d0409c14-2b32-4cdb-9d5d-57a8a9a0818f` (September 23 payment), retaining ID `1966d92d-c3ae-4da1-8daa-483789298533` (October 31 payment). The coordinator found an [issuer-indexed Invesco row](https://www.invesco.com/us/financial-products/etfs/product-detail?audienceType=investors&productId=QQQ&ticker=QQQ) listing ex-date September 19, 2022, record September 20, payment October 31 and $0.51856. Direct opening redirected rather than yielding the historical table. Consequently this supports a **provisional diagnostic convention**, not a fully verified correction. The script guards both exact IDs, dates and amounts before removal and records the evidence limitation in the manifest.

Corrected inputs contain 255 actions, retaining all 90 unknown payment dates. They rebuild a forward total-return index from raw closes and archived ex-date cash distributions; the recurrence is previous index × split ratio × (current raw close + distribution per post-split share) / previous raw close. No cash distribution is inferred from adjusted prices and no unknown payment date is filled. Signal OHLC and scale are consistently derived from that index. The daily engine independently uses split-adjusted raw ATR, not dividend-adjusted ATR. This removes dependence on the provider's QQQ double-distribution adjustment and SPY factor mismatch, but cannot prove the action ledger or historical arrival/publication provenance is correct.

Both bundles remain `qualification_allowed=false`. These conventions enable clearly labeled historical diagnostics; they do not establish a trading edge. Four offline regression tests passed: default ambiguity preservation, explicit exact-record correction, unknown-payment preservation, forward total-return causality and split accounting.
