# Multi-asset trading project roadmap

Accepted September 24, 2026. This is the implementation mandate, not authorization to activate live trading. The user will develop on the Mac, run at least one month of paper trading, then move the persistent runtime to an always-on Windows PC while retaining private remote access and controlled updates from the Mac.

## Investment and deployment mandate

- Objective: long-term absolute growth after trading costs, not a daily profit or trade-count target.
- Research equities/ETFs, options and crypto in parallel. Initial effort allocation: 45%, 35%, 20% respectively; this is not a capital allocation rule.
- Initial future live pilot: $1,000–$5,000 personal funds, no borrowing or short selling. Subscription/infrastructure costs are a separate R&D budget, reported alongside trading returns.
- Proposed pilot controls: 0.25% planned risk/trade, 1% aggregate planned open risk, 50% gross exposure, 10% per issuer and 10% aggregate crypto, 1% UTC-day entry halt, 5% cash-flow-adjusted high-water drawdown suspension/review. These are planned new-pilot controls, NOT modifications to the existing crypto experiment. Gaps can exceed them.
- Options start with long premium only when the full possible premium loss fits the risk budget; borrowing-free does not mean options have no embedded leverage. Broader options structures require separate lifecycle and risk review.
- Software operates continuously; each security obeys its actual session, holiday, settlement, account and entitlement restrictions. Alpaca-supported capabilities are discovered and validated; futures/forex are not invented from public proxy coverage.
- Live credentials, account activation and release authority remain separate from paper research. A calendar date or passing software tests never enables live trading.

## Phases and acceptance

| Phase | Deliverable | Exit gate | Current state |
| --- | --- | --- | --- |
| 0 Mandate and baseline | This roadmap, service inventory, strategy authority register, milestone tracking | Every component has an owner, purpose and activation boundary | Complete September 24: mandate, ownership/authority inventory, capability baseline and dashboard verified; see PHASE0_ACCEPTANCE.md |
| 1 Portable operations | Mac paper operation, WSL2 deployment templates, secrets abstraction, preflight, private access runbook | Demonstrated PC boot/recovery, backups/restore, old-writer fencing and alerts | Manifest/preflight/templates and release receipts implemented; local trial-ledger backup/isolated restore verified; PC not accessed or verified |
| 2 Durable evidence/accounting | SQLite operational trial ledger, immutable research data, source provenance and broker reconciliation | Reconstruct decisions and cash/quantity changes; resolve discrepancies | Operational ledger is first deliverable; full trade accounting remains pending |
| 3 Parallel strategy research | At most two newly frozen hypotheses per asset-class track; causal costs and passive/risk-matched comparisons | Independent review and sufficient untouched prospective evidence | No new hypotheses promoted; existing failures retained |
| 4 Unified execution/risk | One account owner, shared reservations, asset-specific lifecycle adapters and new pilot controls | Fault injection, ownership, partial-fill/unknown-order recovery and accounting tests | Current broker writer remains BTC/ETH-only |
| 5 Prospective paper qualification | At least 30 consecutive operational days plus asset-specific paper records and investment confirmation | Operational AND investment gates pass independently | Observer deployed September 24, 2026 UTC; actual continuity recorded from first observation, no retroactive credit |
| 6 Small live pilot/expansion | Explicitly approved release/account/strategy, limited real capital, measured costs | Reviewed execution and investment evidence before each scale increase | Blocked; no live activation |

Engineering estimate: 8–12 weeks for an initial hardened platform, with overlapping work. Investment validation is evidence-dependent and may take six months or more. Failed experiments do not become passed by repeatedly changing their settings.

## Research and qualification rules

Freeze each hypothesis, universe, parameters, cost model, benchmark, sample period and rejection criteria before examining evaluation outcomes. Track every attempt. Existing MR/MOM failures stay retired; old H1 results remain a failed original hurdle and a research lead, not a qualified strategy.

Preserve the legacy 126-session protocol unchanged. New tracks default to a separately registered 126 relevant sessions (or UTC days for crypto), at least 30 closed episodes, multiple-testing/dependence-aware analysis, stressed execution and a stated risk mandate. The one-month paper requirement is a minimum operational/behavior trial, not a replacement for investment evidence. Equity and options paper clocks do not start merely because their read-only data collectors run.

## One-month trial

The `paper_trial` observer starts recording now, using saved service states only. It cannot access credentials, query the broker or submit orders. It retains restart-safe SQLite observations and checks heartbeat gaps; it does not backfill days preceding its creation. Operational continuity, data coverage, actual paper-execution scope and investment readiness are separately displayed. Existing paper-account equity is an observation, not cash-flow-adjusted profit without the necessary ledger.

A reset of the operational streak never deletes earlier incidents. Paper credentials and database contents remain excluded from Git. At least one month of paper evidence must be gathered before consideration of real money; PC cutover must also complete its own recovery/soak checks.

## Mac development and PC operation

Use Mac development -> reviewed versioned Git commit -> explicit PC deployment. Do not automatically pull main into an active executor. Ubuntu under WSL2 retains POSIX locking and directory durability; this is not native Windows Python support. Keep state on the Linux filesystem and secrets outside the repository.

Private remote access provides dashboard viewing, logs and controlled deployment while the Mac is mobile. No public unauthenticated dashboard or broker-control endpoint. Remote access and PC boot automation must be installed and verified on the actual PC; preparing templates on the Mac is not proof of readiness.

Migration must stop/fence the old account writer, archive and verify state, reconcile broker orders/positions, then start exactly one designated owner. Existing API-key-derived local locks do not fence another machine or another key for the same account. The current Mac writer must not be duplicated by simply cloning and starting scripts on the PC.

## Delivery rhythm

Each milestone has an implementation owner, offline regression evidence, independent review and explicit limitations. Update this roadmap and release notes with completed work. Weekly reports cover equities/ETFs, options and crypto separately, plus accounting, reliability and remaining blockers. Deploy read-only improvements independently; changes to execution policy require their own reviewed rollout.

## Current stop point

Phase 0 is complete; see [PHASE0_ACCEPTANCE.md](PHASE0_ACCEPTANCE.md) for requirement-by-requirement evidence. At the user's request, stop implementation here. Later phases retain the states above; this closure does not activate trading or stop existing authorized services.
