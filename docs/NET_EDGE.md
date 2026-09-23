# Frozen ETF research, review and diagnostic shadow

These commands use local files and the shared ETF strategy/replay modules. They do not connect to a data provider or broker, submit orders, register a formal forward trial, or grant trading approval. Use the project Python environment. Every output location must be new; source files remain unchanged.

## Commands

```
python scripts/run_net_edge.py freeze --output artifacts/net_edge/registry.json
python scripts/run_net_edge.py freeze --output artifacts/net_edge/known-cost-registry.json --monthly-overhead 75
python scripts/run_net_edge.py research --data PATH_TO_DATA --registry PATH_TO_REGISTRY --output NEW_RESEARCH_DIR
python scripts/run_net_edge.py research --data PATH_TO_DATA --registry PATH_TO_REGISTRY --output NEW_DIAGNOSTIC_DIR --diagnostic-last 20
python scripts/run_net_edge.py analysis --input SNAPSHOT.json --output NEW_SPECIALISTS_DIR
python scripts/run_net_edge.py shadow --input SHADOW.json --registry PATH_TO_REGISTRY --candidate MR30 --output NEW_SHADOW_DIR
python scripts/run_net_edge.py review --bundle EVIDENCE_DIR --as-of YYYY-MM-DD --output NEW_REVIEW_DIR
```

Freeze registers four development definitions: MR30, MR60, MOM20, MOM60, with the fixed SPY/QQQ/IWM/TLT/GLD universe. It stores specification and source hashes, UTC freeze time, and overhead. An optional `--frozen-at` must have a timezone and cannot be future-dated. Changing source after freeze requires a new development registry. Historical freeze records are not independently authenticated preregistration. Freeze does not begin confirmation.

Omitting monthly overhead means **unknown**, not zero. Explicit `--monthly-overhead 0` means the operator asserts no operating expense. Calendar allocation uses each month's actual number of days, including weekends and holidays inside the evaluated interval, rather than charging only trading days. Historical replay reports broker-like net trading P&L separately from business P&L after overhead; its fills are simulated, never actual broker fills.

Research runs shared continuous replay at 1/3/7/14/28 bps per side and 14 bps plus one additional minute of delay. It uses disjoint 252-session training, 63-session validation and 63-session test folds with five-session gaps at both boundaries, retaining cash and positions through the stitched test period. No complete fold (388 sessions needed for the first), or explicit `--diagnostic-last`, produces diagnostic results that cannot qualify. MOM additionally needs enough daily warm-up history. No new simulator is introduced.

Research produces `summary.json`, copied registry/data manifest, per-candidate cost/delay directories containing `metrics.json`, `ledger.parquet`, `daily.parquet`, `trades.parquet`, `decisions.parquet`, and symbol contributions. `benchmarks_7bps.parquet` compares idle cash with an initially 50%-invested equal-weight buy/hold ETF basket, raw split/distribution accounting, 7 bps entry and terminal hypothetical liquidation costs, and calendar overhead. The basket does not rebalance daily. Missing benchmark symbol-session observations block output rather than inventing marks.

Unknown overhead, unverified/ incomplete data coverage or open-position valuation, unverified corporate actions or cost calibration, fewer than 126 OOS sessions, insufficient closed episodes, nonpositive economics, concentration in the best five days, or risk-limit breaches prevent development qualification. Preliminary failures skip costly bootstrap and leave-one-ETF-out runs. Otherwise five exclusion replays must each remain profitable, and joint stationary bootstrap of base/double-cost/adverse-delay daily vectors uses 100,000 resamples, blocks 5 and 10, and alpha 0.05/6. Cross-series dependence is retained. Passing historical screening is still development, not prospective confirmation or permission to trade. All artifact directories have an `artifact_index.json` with file hashes.

## Historical dataset schema

`manifest.json` contains a `hashes` object mapping relative filenames to SHA-256. Paths cannot escape the data directory. All required files must be listed and verified. Optional `corporate_actions_verified` and `cost_calibration_verified` must be literal true for qualification; these local metadata assertions are not independent authentication.

- `minutes/{SYMBOL}.parquet`: unique ascending timezone-aware DatetimeIndex; numeric `open, high, low, close, volume`. Index marks the minute's observation/open time; close becomes available one minute later. Raw prices execute trades.
- `daily/{SYMBOL}.parquet`: unique ascending timezone-aware completed-daily index; `high, low, close, total_return_close, signal_scale`. Daily history supports MOM signals. `signal_scale` converts adjusted ATR back to executable raw-price scale. Do not fabricate total-return prices or missing scale.
- `schedule.parquet`: ascending unique `market_open, market_close`, both timezone-aware. Supply actual exchange sessions and early closes; no weekday-only replacement.
- Optional `actions.parquet`: ascending timezone-aware event index and `symbol, split_ratio, cash_dividend`. Dividend is per post-split share. Empty, verified actions are distinct from absent/unverified actions. Missing actions prevent qualification; diagnostics disclose the gap.

The loader verifies archived hashes, not provider authenticity or economic correctness. The upstream downloader/normalizer must preserve acquisition source, adjustment conventions and source limitations.

## Specialist analysis snapshot

JSON fields:

```
{
  "as_of": "2026-09-23T19:00:00Z",
  "strategy_hash": "64 lowercase hexadecimal characters",
  "symbols": ["SPY", "QQQ", "IWM", "TLT", "GLD"],
  "max_age_seconds": 86400,
  "max_age_by_kind": {"technical": 120},
  "evidence": [{
    "source_id": "unique-id",
    "source_url": "https://source.example/record",
    "kind": "technical",
    "observed_at": "2026-09-23T18:59:00Z",
    "published_at": "2026-09-23T18:59:00Z",
    "available_at": "2026-09-23T18:59:00Z",
    "vintage_at": null,
    "data": {"symbol": "SPY", "closes": [100, 101], "vwap": 100.5}
  }]
}
```

`vintage_at` is optional. Every supplied timestamp must include timezone; future/stale evidence is rejected using availability, publication, observation and optional vintage. Kind-specific limits remain explicit. Other supported evidence kinds are `etf_fundamental` (expense_ratio/nav/holdings), `news` (headline) and `macro` (series/value/release_at matching publication). Evidence is supplied data, never agent instructions.

Analysis writes `team_report.json`, with technical/fundamental/news/macro/bear reports, source IDs and URLs, timestamp provenance, missing-data reasons and `method: deterministic_evidence_analysis`. These are local deterministic advisory specialists, **not five live LLM calls**. No API provider is configured; external model integration awaits the credential decision. Advisory views cannot change frozen strategy or risk gates. Empty evidence produces insufficient-data reports, not invented facts.

## Diagnostic shadow snapshot

`shadow` reads a JSON snapshot with these top-level fields:

- `context`: the complete analysis snapshot object above. `strategy_hash` must match the selected candidate from the verified registry; `symbols` must contain all five ETFs.
- `histories`: `{ "SPY": "histories/SPY.parquet", ... }` for all five ETFs. Paths are relative to the snapshot directory; absolute paths, parent traversal and escaping symlinks are rejected. MR histories contain completed current-session minute OHLCV; MOM histories contain completed daily columns described above.
- `quotes`: optional map by symbol. Each value has `feed` (SIP expected), `received_at`, and `raw_payload` containing `t`, `bp`, `ap`. Decision time is recomputed from `context.as_of`; caller-supplied validity cannot override validation. Missing, stale, crossed or wrong-feed quotes block entry proposals.
- `risk_state`: relative path to persisted AccountRisk JSON. Required fields are `version: 1`, numeric-string `equity, highwater, daily_baseline, cashflow_total`, ISO session date `session`, boolean `daily_halt, drawdown_halt`. Existing halts remain latched; existing breaches are re-evaluated in a temporary copy. Missing/invalid state cannot approve entries. Original risk state is never modified.
- `positions`: list of existing long whole-share exposures, each with `symbol, quantity, price, stop_price`, optionally `overnight` and `exit_cost_per_share`. Empty list is an explicit assertion of no holdings.
- `cash`: known nonnegative available cash.
- `session_open`, `session_close`: explicit timezone-aware actual exchange boundaries.
- For MOM, also `previous_session_close` and `history_available_at` map of symbol to timezone-aware availability timestamp. These establish completed-daily availability; missing values block MOM proposals.

The runner checks strategy identity, completed-bar availability, signal freshness, SIP quote health, execution window, portfolio exposures and durable risk state. It produces proposed quantities and rejection reasons, never fills or orders. Five local specialists are attached but cannot override decisions. Output includes `manifest.json`, `decisions.json`, `advisory.json`, `team_report.json`, source hashes in `inputs.json`, and artifact index. `formal_forward_run` is always false. A saved snapshot is one diagnostic observation, not an uninterrupted monitored session or proof of broker protection.

## Formal evidence review

`review` consumes an already assembled fixed-window bundle. It does not collect observations or register/extend a trial. Required files: `evidence_manifest.json`, `protocol.json`, `calendar.json`, `daily.json`, `marks.json`, `leave_one_out.json`, `engineering.json`, `data_quality.json`, `cost_calibration.json`, plus referenced supporting files. Tables are JSON records; exclusion replay data maps each ETF to its daily business-P&L vector. Manifest files must include and hash all inputs. The provided tables are bound to those archived files.

Protocol freezes exactly 126 exchange sessions after freeze, $100,000 initial capital, explicit monthly overhead or unknown, minimum 30 closed position episodes, 3% maximum observed drawdown, 0.5% daily halt, bootstrap blocks/resamples/alpha/seed, and strategy/code/calendar hashes. No early pass, incomplete-window pass or automatic extension. Calendar list/hash determines sessions, including holidays. Unknown overhead is inconclusive.

Daily columns: `session, gross_pnl, execution_cost, net_pnl, operating_cost, business_pnl, equity, business_equity, stressed_business_pnl, adverse_business_pnl, closed_episodes, reconciled, halt_compliant, valuation_valid`. Equity includes open-position marks; idle-session overhead still accrues. Mark columns: `timestamp, session, equity, day_start_equity, halt_compliant, entries_blocked`. Daily and business identities, exact overhead allocation, nonnegative costs, finite values, calendar coverage, marked drawdown and latched halt compliance are checked. Canceled orders with partial fills must remain in reconciliation; fees are charged once.

Each report requires `passed`, matching `strategy_hash`, named `checks` records with literal boolean `passed`, and references in `artifacts` to manifest-listed files. Required check names:

- Engineering: signal_parity, halt_and_drawdown, partial_canceled_fills_included, no_fee_double_count, replay_shadow_parity, future_price_causality, future_missingness_causality, continuous_nonoverlap_portfolio, order_transition_races, duplicate_lost_ack_restart, overnight_split_dividend_calendar_dst, cash_position_fee_reconciliation, stale_data_unknown_ownership_blocking, emergency_seven_position_load, partial_fill_protection, overnight_restart_adoption.
- Data: exchange_calendar, corporate_actions, open_position_valuation, complete_sessions.
- Cost: quotes_and_latency, double_cost_replay, adverse_delay_replay, leave_one_out_replays.

Result is pass/fail/inconclusive; `approved_for_trading` remains false. Integrity checks and local reports cannot establish provider authenticity, prove unobserved continuous risk compliance, guarantee fillability or guarantee future returns. Drawdown/halt triggers cannot cap losses through gaps. The observed ledger remains the unit of evidence; simulation resamples are not independent new market sessions.

Engineering checks each additionally require their own nonempty `artifacts` list referencing supporting files in the hash-verified manifest. The emergency load check requires `positions >= 7`, `mode: normal_simulation`, and finite numeric `measured_seconds <= 1.0`. A report-level pass does not substitute for any named check. In particular, unresolved protection of partially filled orders and overnight restart adoption remain explicit engineering blockers. This workflow never creates an engineering pass report from its own limited unit-test count. Hashes establish file integrity, not that a claimed test or broker guarantee is true.

## Current integration limits

The September 23 build is a research and diagnostic-shadow release. Production collection/runner integration and broker-connected partial-fill/overnight validation remain pending. A partial entry is canceled immediately, but cancellation uncertainty can leave exposure temporarily unprotected; this is an explicit engineering blocker. The account-wide risk module is exercised in shadow, not a claim that every legacy paper runner has adopted it.

Corporate-action inputs may include payment_timestamp: ex-date accrues a receivable, and only a supplied payment timestamp releases cash. A missing timestamp leaves the entitlement unspendable even after the position is sold. The connected archive importer uses 09:30 New York on a documented payable date as a diagnostic convention; exact broker credit time needs reconciliation. The passive benchmark uses theoretical fractional units to fix initial equal weights; executable strategy entries use whole shares.
