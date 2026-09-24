# September 23 review and next evidence gate

## Review checkpoint

Commit `7ab83c5` preserves the entry-stop correction, sensitivity core, YTD runners/tests and existing agent documentation. Focused checkpoint checks: 33 passed. Full checkpoint suite: 620 passed (one environment core-count warning). This is a research checkpoint, not broker-readiness approval.

## Stop correction

The initial-stop fill clamp must also apply when reserving remaining stop risk for positions entered earlier in the same bar. A regression reproduced $216.958 remaining liquidation risk against a $99.783 portfolio budget before correction. Python and C++ now cap the stop valuation at the executable mark. Per-trade costs include entry commission and adverse exit impact/commission.

Diagnostic shadow clamps initial stop execution to the current bid and carries the resulting exit cost into subsequent proposals. AccountRisk accepts an explicit executable reference; absent one it reverses the configured impact from the assumed impact-adjusted entry price. Do not pass an unadjusted price with a nonzero impact and assume it means the same thing.

These checks establish conservative initial-stop handling, not full replay/shadow parity. Shadow uses entry-price exposures and fixed account equity; replay marks exposures and subtracts entry costs. Caller-supplied positions must retain correct current exit-cost evidence; rows without it retain the existing nominal-stop fallback. Partial-fill protection, account-wide recovery wiring and broker validation remain separate release blockers.

## Quote evidence

Accepted and rejected entry, submission and holding quote decisions now retain raw payloads, source and receipt/decision times, feed, policy and exact validation reasons. Shadow retains diagnostics even for missing quotes and symbol mismatches. Nonfinite raw values retain replayable token text while outer evidence remains strict JSON. Evidence write failures stop use of the quote. See [QUOTE_EVIDENCE.md](QUOTE_EVIDENCE.md) for scope, preserved gates and fixture provenance. Original XOM/TJX payloads remain unavailable.

## MR30 versus MR60

The saved 7bps/no-delay baseline has 74 trades for each candidate: 71 stop exits, three targets, maximum holding time 26 minutes. Sixty-one trades close in their entry minute. Saved lower-cost scenarios do have time exits at exactly 30 versus 60 minutes, supporting the intended timeout distinction.

A separate execution defect invalidates reliance on the original gross-profit claim: 42 initial stops exceed the raw entry open and were filled at the favorable nominal stop in the original artifacts; 23 raw stop fills exceed that bar's high. Repricing those stops at fixed saved quantities lowers gross P&L by $81.984145, more than the original $38.70 gross gain. This arithmetic is not a corrected portfolio replay: corrected sizing and subsequent decisions can change results.

The tracked per-trade listing is in [SEPT23_MR_EXITS.md](SEPT23_MR_EXITS.md). Detailed reconstructed causal barriers, machine-readable rows and hashes are in `artifacts/mr_exit_audit_20260923/`. Original artifacts are retained. Saved campaigns predate the aggregate-risk correction and must not be relabeled as current-code runs.

## Market data decision

The existing account's recent SIP quote request returned HTTP 403 in `artifacts/net_edge_20260923/REPORT.md`. Alpaca's Basic plan provides real-time equities data from IEX only; Algo Trader Plus lists all US exchanges at $99/month. The subscription/access decision belongs to the user. No purchase, feed fallback or subscription change was made. Quote-sensitive execution needs validated consolidated executable quotes; alternatively, define and test a strategy that does not depend on unavailable quote coverage.

An IEX decision feed versus NBBO paper fill reference is a plausible explanation for quote/fill disagreement. It does not prove the cause of KLAC's discrepancy or XOM/TJX's validation failures. The rejected incident payloads were not retained, so an exact historical reproduction is impossible from current evidence. Offline fixtures reproduce labeled failure categories, not invented historical quotes.

Source, checked September 23: [Alpaca data plans](https://docs.alpaca.markets/us/docs/about-market-data-api).

## Paper assumptions and the 25-cent difference

Alpaca states that paper trading omits market impact, queue position, latency slippage and regulatory fees; it also omits price improvement. Paper fills can exceed displayed NBBO size and are simulated against NBBO. Those assumptions can favor rapid/large orders, but paper P&L is not a mathematical upper bound on every possible live result. The live counterfactual cannot be calculated from today's order export alone.

The account-versus-fill/history difference remains $0.25 with no documented explanation. A one-time read-only recheck is scheduled for September 24 at noon Eastern. Delayed activity creation is possible in the activity API, but regulatory fees must not be assumed to explain a paper-account gap.

Sources: [paper rules](https://docs.alpaca.markets/us/docs/paper-trading), [activity API](https://docs.alpaca.markets/us/reference/getaccountactivities-2).

## Next research gate

No new roles or infrastructure are needed for the next experiment. Before another large sensitivity campaign:

1. Freeze a small set of hypotheses with fewer trades and longer holding periods. Specify entry, exit, availability and failure conditions before reading evaluation-period results.
2. Screen corrected before-cost expectancy against the same buy-and-hold ETF basket, dates, capital basis, corporate actions and valuation rules. Gross advantage is necessary evidence to investigate, not sufficient evidence to trade.
3. Reject ideas whose gross edge is absent or too small for a plausible cost budget. Then apply measured spread, impact, delay and commissions plus explicit subscription/operating expenses.
4. Retain failed candidates and untouched later evaluation periods. Report turnover, concentration, exposure, drawdown, sample size and regime coverage alongside basket-relative return.
5. Keep load/throughput tests in a separate report. Higher order count and a profitable fraction of cost assumptions do not establish market edge.

No new broker actions, trading restart or live-market experiment was performed for this review.

## Final repair validation

- Full offline suite: `.venv/bin/python -m pytest -q -p no:cacheprovider` — 653 passed in 77.45 seconds; one pre-existing joblib physical-core detection warning.
- Focused stop/replay/sensitivity/YTD checks: 58 passed; shadow/workflow diagnostics: 16 passed; quote/holding/recovery/runner checks: 75 passed. These sets overlap and must not be added together.
- New regressions failed before the stop fix, including the aggregate budget violation, and passed after it.
- Independent source review found no blocking findings in the stop or quote-evidence changes. The reviewer inspected tests but did not independently execute them.
- `git diff --check` passed. No broker, subscription or runner actions were taken.
