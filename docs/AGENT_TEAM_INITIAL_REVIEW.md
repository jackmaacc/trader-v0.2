# Initial project-agent review — 2026-09-23

Three Codex subagents completed bounded read-only assessments of the current repository during team setup. They inspected code and documentation; they did not run trading jobs or verify broker behavior. Findings below are backlog items, not implemented fixes. The existing five application advisory roles remain unchanged.

## First assignments

| Priority | Assignment and evidence | Owner and completion check |
|---|---|---|
| 1 | Shared account recovery: `scripts/paper_portfolio_until_close.py:107-110` does not pass the shared recovery policy or consult its global freeze; `src/trader_engine/execution/recovery.py:20-26` describes account-wide sharing. | execution_engineer + portfolio_risk, then test_engineer: simulate one symbol entering uncertain protection, freeze new entries across the account, preserve exits/protection, and require reconciliation before clearing. |
| 1 | Partial-entry protection remains unresolved: `src/trader_engine/execution/lifecycle.py:237-280`; documented in `docs/NET_EDGE.md:97-101`. | execution_engineer + independent_reviewer: reproduce partial fill, failed cancellation, delayed fills, lost acknowledgment and restart offline; retain explicit uncertainty about broker guarantees. |
| 2 | Quote evidence can retain mutable raw dictionaries in directly constructed envelopes: `src/trader_engine/data/quote_validation.py:29-35,73,124`; batch path alone deep-copies at line 138. | data_quality + test_engineer: test direct-envelope mutation after validation as well as batch isolation. Determine desired immutable evidence contract before editing callers. |
| 2 | Cache boundary consistency: `src/trader_engine/data/cache.py:19-29` reads/stores without bar validation; `data/providers.py:63-68` returns cached frames directly; `data/base.py:48-67` supplies validation. | data_quality: validate malformed/corrupt cache cases. Downstream validation exists, so do not claim all malformed cached data currently reaches execution. |
| 2 | Learned-state causality investigation: `research/context.py:122-124` fits the full input; `workflows/research.py:38-45` uses classifications; walk-forward separately refits training slices at `research/walk_forward.py:112-145`. Paths are under `src/trader_engine/`. | backtest_auditor + research_designer: perturb future observations and identify exactly which models and workflows change earlier classifications. This is not yet a confirmed defect. |
| 2 | Walk-forward tests at `tests/test_robustness.py:111-146` establish output shape, not the full causality invariant. | test_engineer: show that test-window perturbations cannot alter training-derived classifications or validation signals, including optional validation refitting. |
| 3 | Account-risk integration differs between legacy experiments and diagnostic shadow (`scripts/paper_portfolio_until_close.py:89,110`; `docs/NET_EDGE.md:101`). | portfolio_risk: map each runner to its intended, user-approved policy before proposing integration. A disabled session-loss cap in the legacy experiment is not itself an accidental-policy bug. |
| 3 | Quote batch isolation and timestamp boundary coverage (`src/trader_engine/data/quote_validation.py:85,104,128-139`). | data_quality + test_engineer: pair malformed timestamps/oversized inputs with a healthy symbol and reproduce any escaping failure before reporting it as a bug. |
| 3 | Reproducibility: dependencies have lower bounds in `pyproject.toml:11-20`; no existing `.github` workflow or dependency lock was found by the reviewer. | reliability_engineer: propose an environment manifest, bounded offline checks and synthetic profiling; optimize only after measuring. |

## How to execute this backlog

The coordinator chooses one bounded outcome, assigns exclusive files to writers, and uses up to the runtime's available slots. Final execution/risk changes receive independent review after the patch stabilizes. Findings are starting evidence from this date; recheck line references and current state before implementation because other project tasks may continue changing the repository.

## Setup validation

Validate the ten TOML files with Python's `tomllib`, require unique matching file/name values and nonempty description/instructions, and check the roster against actual definitions. This verifies the saved configuration syntax and inventory. It does not prove that an already-running host has loaded the named roles. The three initial workers were actually dispatched through the current session's collaboration tools with explicit role assignments.
