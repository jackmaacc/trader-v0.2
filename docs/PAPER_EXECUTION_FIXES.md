# Paper execution repair — September 23, 2026

The fifteen-minute throughput experiment stopped twice because the disk rejected writes. It produced 136 filled orders / 68 completed round trips and lost $2.72. Both remaining positions were recovered; the account was verified flat at $99,986.85. Engineering verification initially used offline tests only. After the subsequent request to test before the close, three supervised paper round trips completed at 14:39 Eastern; see the results below.

## Defects and changes

1. The old worker wrote request telemetry before returning the broker response. A logging failure therefore hid successful orders and prevented recovery reads. The new adapter decodes broker responses independently of best-effort telemetry. The lifecycle retains acknowledgments in memory before logging.
2. The old worker assigned ownership only after a fallible order snapshot. The new executor durably saves its ownership plan and deterministic entry/exit IDs before submitting an entry. Entry intent writes must succeed. Once exposure is accepted, a storage failure disables subsequent entries while confirmed exits bypass further persistence requirements.
3. Exit state lived in a short-lived local list. The new executor reconciles all reserved exit IDs before sending another exit, confirms cancellation terminal states, and subtracts cumulative sell fills from final buy fills. Ambiguous POSTs are looked up and never blindly retried.
4. Reconciliation distinguishes an absent order from an unavailable service. Only a genuine order-not-found HTTP response means absent. Known orders get bounded visibility retries. Identity mismatches, overfills, regressing quantities, foreign orders on the same symbol and inconsistent holdings require reconciliation.
5. Logging grew through repeated history rewrites and disk probes could become stale immediately. The reserved journal allocates bounded storage before exposure, checksums records, flushes durable intents, refuses torn/corrupt records and prevents concurrent writers. An account lock serializes the new runner across separate journals on this host. Preallocation still cannot guarantee later I/O success; the exit path remains independent of it.
6. The one-share constant is replaced by a pure configurable sizing function. The dormant profile targets $7,500, capped at $10,000 per symbol and $50,000 gross, with additional 10%/50% actual-equity caps. Cash, pending reservations, modeled stop risk and a $1,000 marked session-loss cutoff can reduce the size. Leveraged buying power is never treated as equity.
7. The paper adapter refuses mutations by default, hardcodes the paper origin, blocks redirects, spaces requests and reserves rate capacity for recovery. No automatic POST retry, real-money endpoint, scheduler or automatic strategy promotion is included.

## How to review without trading

```sh
.venv/bin/python scripts/paper_execution.py --symbol SPY --limit-price 100 --stop-price 99
.venv/bin/python -m pytest tests/test_paper_*.py -q
```

The default command is an offline calculation using supplied account values. At $100,000 equity and $100/share, the default profile returns 75 shares ($7,500), subject to the limits. That price is an example, not a quote or stock recommendation. The stop price is a sizing assumption; it is not a submitted protective stop.

The separate `--execute-paper` flag is an explicit manual single-round-trip experiment. It requires environment credentials and a **new** `--journal` path, reads actual account equity/cash, requires a flat paper account and at least ten minutes left in the regular session, holds an account lock, and immediately closes the entry. It installs non-raising SIGINT/SIGTERM stop handlers around order execution. It does not implement a predictive strategy, portfolio deployment or an unattended high-frequency loop. After the offline suite passed, the explicitly requested before-close shakedown used this flag for three supervised single-round-trip tests. No ongoing trading worker was started.

## Failure coverage and remaining limits

Fault-injection tests cover disk-full before/after buy acknowledgment and during exits, exhausted journal capacity, lost POST responses, delayed order visibility, cancellation/fill races, partial fills, duplicate-process locks, corrupt records, missing average fill prices, unrelated activity, stop signals, rate limiting, redirect refusal and mutation-off defaults. These are deterministic engineering tests, not market-performance simulations.

A process kill, machine sleep/power failure or broker outage can still prevent timely exits. Recovery will inspect existing orders and cancel owned working orders when explicitly invoked, but will **not submit a missing exit after restart** when the available evidence cannot prove it was never sent. That case requires direct broker reconciliation. The new manual runner reports unresolved status instead of claiming completion. Automatic restart is intentionally unavailable; an independent supervised recovery service and broker-held protection are still required before any unattended trading deployment.

The workstation repeatedly returned `ENOSPC` despite reporting some free space. No unrelated files or historical research evidence were deleted. Journal reservation and fault handling address the execution defect, but available disk capacity still needs restoring before a sustained broker experiment. Larger sizes and more orders do not establish profitability; the previous strategy research has not demonstrated a consistent net edge.

Original evidence: `/private/tmp/trader2-paper-test/BURST_FINAL_REPORT.md` and `/private/tmp/trader2-paper-test/burst_final_verification.json`. Old one-off burst scripts and archived executors are retained as evidence and must not be reused for trading.

## Final verification

The complete regression suite passed: **394 tests**, including **219 new execution/sizing checks**. Fault injections used a fake broker, never real orders. The repaired adapter also passed a read-only paper-account check with mutations disabled.

The subsequent authorized before-close broker test completed at **14:39:28 Eastern**, with three round trips and six orders receiving fills. Requested entry values were $6,910.92 in SPY, $7,404.70 in QQQ and $7,436.88 in NVDA. SPY filled nine of nine shares; QQQ filled one of ten; NVDA filled seven of thirty-three. The latter IOC orders canceled their unfilled quantities; exits matched the quantities actually filled. Actual entry values were $6,909.39, $740.37 and $1,577.17 respectively.

The account declined **$0.47**, from $99,986.85 to **$99,986.38**, matching fill P&L to cent precision. All positions and orders were verified closed. No storage errors, duplicate exits or unresolved statuses occurred. This establishes limited broker integration evidence, not profitability or unattended deployment readiness. See [the broker-test report](../artifacts/paper_repair_shakedown_2026-09-23/REPORT.md). Trading and both earlier heartbeat monitors remain stopped.
