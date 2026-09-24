# Research decisions — September 23, 2026

## Accepted direction

The user selected the free-data route. A shortlist of four concrete hypotheses is prepared for review before any further backtests. The recommended shift to completed daily/weekly information, slower holdings, low turnover, explicit same-date buy-and-hold comparison, and a genuinely untouched later period remains pending user confirmation. No paid-data purchase, subscription change, broker action or runner change follows from that choice.

MR30, MR60, MOM20 and MOM60 failed the prior evidence gates and remain nonpromotable. Formal retirement with no retuning is recommended and awaits user confirmation; no retuning or recycling under new names is authorized by this task. Their saved artifacts remain available, including the per-trade MR exit audit in `docs/SEPT23_MR_EXITS.md`. Fixing a simulation defect is an engineering correction; it is not evidence that a rejected strategy is promising or permission to rerun a search.

## Proposed, awaiting approval

`docs/NEXT_HYPOTHESES.md` supplies exactly four fixed proposals: monthly trend filter, weekly relative-strength rotation, daily breakout with longer holding, and monthly inverse-volatility allocation. Rules, data gaps, sizing, timing, costs, cash/dividends, benchmarks and rejection criteria are specified there. These are unapproved drafts, not registered frozen experiments. Monthly candidates may hold for months; the slower duration is explicit.

Before-cost expectancy for every proposal: **NOT YET MEASURED**. No hypothetical performance numbers or simulated observations were produced to fill the user's requested shortlist. The economic rationale for each proposal is a falsifiable idea, not an expected profit estimate.

The proposed primary benchmark is 100% equal-weight buy-and-hold SPY/QQQ/IWM/TLT/GLD, using the same $100,000 and exact dates, with consistent cash-distribution accounting. A 25%-invested/75%-cash version is secondary because strategy proposals preserve the existing 25% overnight cap. The smaller benchmark cannot replace the primary after results. Lower strategy exposure and any drawdown advantage must be disclosed; raw-return underperformance alone does not prove absence of risk-adjusted benefit. No account risk setting changes here.

Proposed windows: 2016–2023 development, 2024–September 23, 2026 consumed diagnostic history, and October 1, 2026–September 30, 2027 prospective confirmation. Already-examined dates remain consumed even for a new hypothesis. The prospective candidate and protocol must be fixed before the first future session, without inspecting confirmation results for selection. If preparation misses that deadline, the proposed interval cannot serve as untouched confirmation for a later-selected candidate; agree a new future interval before using it. No holdout is currently registered or started.

The proposed year-long design requires explicit reconciliation with the existing 126-session formal-review protocol before registration. Writing a new proposal does not change the existing review machinery or imply that it accepts new parameters. Do not build another framework to bypass this decision.

## Coverage facts verified without outcome inspection

Parquet index metadata for all five archived daily files reports 2,696 rows, January 4, 2016–September 23, 2026. The manifest classifies the archive as development history, leaves cost calibration unverified, marks overall coverage incomplete and states corporate-action publication times are unknown. Existing minute/calendar coverage from 2024 does not establish complete executable daily opens/calendar/actions for 2016–2023. Data eligibility remains an explicit prerequisite to any later approved backtest. No new data was downloaded; price outcomes for the new hypotheses were not inspected.

## Work boundary and next decision

Completed: a written shortlist and evaluation proposal only. Not completed or authorized by this writing task: candidate implementation, parameter search, historical replay, statistical result, paid plan, provider credentials, prospective automation, paper/live orders, or promotion.

Next decision: approve or revise the concrete hypothesis rules, primary benchmark, fixed data windows and rejection/confirmation criteria before freezing or running anything. If the user changes a rule before any outcome is inspected, document the replacement transparently; after outcomes are seen, a changed rule is a new attempted hypothesis requiring a new future confirmation interval. A failed test is retained, not tuned until it passes.
