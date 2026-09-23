from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from trader_engine.research.context import ResearchContext
from trader_engine.research.model_comparison import StateModelComparisonRunner
from trader_engine.research.parameters import apply_parameter_overrides
from trader_engine.research.walk_forward import WalkForwardRunner


@dataclass
class SelectionPolicyComparisonResult:
    summary: pd.DataFrame
    fold_metrics: pd.DataFrame
    experiment_outputs: dict[str, dict[str, pd.DataFrame]]


class SelectionPolicyComparisonRunner:
    def __init__(self, context: ResearchContext) -> None:
        self.context = context
        self.config = context.config
        self.policy_config = context.config.research.selection_policy_comparison
        self.model_items = {
            item.name: item for item in context.config.research.state_model_comparison.models
        }

    def run(
        self,
        members_by_symbol: dict[str, object],
        raw_by_symbol: dict[str, pd.DataFrame],
    ) -> SelectionPolicyComparisonResult:
        summary_records: list[dict[str, object]] = []
        fold_metric_frames: list[pd.DataFrame] = []
        experiment_outputs: dict[str, dict[str, pd.DataFrame]] = {}

        for experiment in self.policy_config.experiments:
            model_item = self.model_items.get(experiment.model_name)
            if model_item is None:
                continue
            candidate_overrides = dict(model_item.overrides)
            selection_policy = experiment.selection_policy or model_item.selection_policy
            if selection_policy:
                candidate_overrides["signals.selection_policy_name"] = selection_policy
            candidate_overrides.update(experiment.overrides)
            candidate_config = apply_parameter_overrides(self.config, candidate_overrides)
            candidate_context = ResearchContext(candidate_config)
            dataset = candidate_context.prepare_dataset(
                members_by_symbol=members_by_symbol,
                raw_by_symbol=raw_by_symbol,
            )
            walk_forward = WalkForwardRunner(candidate_context).run(dataset)

            if not walk_forward.fold_metrics.empty:
                fold_metric_frames.append(
                    walk_forward.fold_metrics.assign(
                        model_name=experiment.model_name,
                        experiment_name=experiment.name,
                        selection_policy=selection_policy or "",
                        category=experiment.category or "",
                        combo_name=experiment.name,
                    )
                )

            experiment_outputs[experiment.name] = {
                "aggregate_metrics": walk_forward.aggregate_metrics,
                **walk_forward.quality_frames,
            }
            summary_records.append(
                self._summary_record(
                    experiment_name=experiment.name,
                    model_name=experiment.model_name,
                    category=experiment.category,
                    description=experiment.description,
                    selection_policy=selection_policy,
                    candidate_config=candidate_config,
                    aggregate_metrics=walk_forward.aggregate_metrics,
                    quality_frames=walk_forward.quality_frames,
                    processed_symbols=dataset.processed_symbols,
                )
            )

        summary = pd.DataFrame(summary_records)
        if not summary.empty:
            summary = self._score_summary(summary)
        fold_metrics = pd.concat(fold_metric_frames, ignore_index=True) if fold_metric_frames else pd.DataFrame()
        return SelectionPolicyComparisonResult(
            summary=summary,
            fold_metrics=fold_metrics,
            experiment_outputs=experiment_outputs,
        )

    def _summary_record(
        self,
        experiment_name: str,
        model_name: str,
        category: str | None,
        description: str | None,
        selection_policy: str | None,
        candidate_config,
        aggregate_metrics: pd.DataFrame,
        quality_frames: dict[str, pd.DataFrame],
        processed_symbols: int,
    ) -> dict[str, object]:
        record: dict[str, object] = {
            "experiment_name": experiment_name,
            "combo_name": experiment_name,
            "model_name": model_name,
            "category": category or "",
            "description": description or "",
            "selection_policy": selection_policy or candidate_config.signals.selection_policy_name or "",
            "selection_mode": candidate_config.signals.selection_mode,
            "quality_gate_components": ",".join(candidate_config.signals.quality_gate_components),
            "hard_filter_components": ",".join(candidate_config.signals.hard_filter_components),
            "family_filter_mode": candidate_config.signals.family_filter_mode,
            "top_n_per_rebalance_date": candidate_config.signals.top_n_per_rebalance_date or 0,
            "top_n_per_asset_class": candidate_config.signals.top_n_per_asset_class or 0,
            "top_n_per_model": candidate_config.signals.top_n_per_model or 0,
            "processed_symbols": processed_symbols,
        }
        record.update(StateModelComparisonRunner._flatten_aggregate_metrics(aggregate_metrics))
        record.update(self._selection_stats("test", quality_frames))
        record.update(self._selection_stats("validation", quality_frames))
        return record

    @staticmethod
    def _selection_stats(split: str, quality_frames: dict[str, pd.DataFrame]) -> dict[str, float]:
        prefix = f"{split}_"
        candidates = quality_frames.get(f"{split}_signal_candidates", pd.DataFrame())
        if candidates.empty or "candidate_signal" not in candidates.columns:
            return {
                f"candidate_count_{split}": 0.0,
                f"pre_cap_trade_count_{split}": 0.0,
                f"post_cap_trade_count_{split}": 0.0,
                f"selected_candidate_ratio_{split}": 0.0,
                f"hard_blocked_rate_{split}": 0.0,
            }
        active = candidates.loc[candidates["candidate_signal"].isin(["long", "short"])].copy()
        candidate_count = float(len(active))
        pre_cap = float(active["selection_passed"].fillna(False).astype(bool).sum())
        post_cap = float(active["final_selection_passed"].fillna(False).astype(bool).sum())
        hard_blocked = float((~active["hard_filter_passed"].fillna(False).astype(bool)).sum())
        family_summary = quality_frames.get(f"{split}_family_filter_summary", pd.DataFrame())
        mean_family_selected_rate = (
            float(family_summary["selected_rate"].mean()) if not family_summary.empty else 0.0
        )
        return {
            f"candidate_count_{split}": candidate_count,
            f"pre_cap_trade_count_{split}": pre_cap,
            f"post_cap_trade_count_{split}": post_cap,
            f"selected_candidate_ratio_{split}": post_cap / candidate_count if candidate_count else 0.0,
            f"hard_blocked_rate_{split}": hard_blocked / candidate_count if candidate_count else 0.0,
            f"mean_family_selected_rate_{split}": mean_family_selected_rate,
        }

    def _score_summary(self, summary: pd.DataFrame) -> pd.DataFrame:
        scored = summary.copy()
        ranking_weights = self.policy_config.ranking_weights
        metric_columns = [column for column in ranking_weights if column in scored.columns]
        scored["policy_score"] = StateModelComparisonRunner._blended_score(scored, metric_columns, ranking_weights)
        scored = scored.sort_values(
            ["policy_score", "mean_test_sharpe", "mean_test_total_return"],
            ascending=[False, False, False],
            na_position="last",
        ).reset_index(drop=True)
        scored.insert(0, "comparison_rank", range(1, len(scored) + 1))
        return scored
