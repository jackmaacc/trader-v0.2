from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from trader_engine.research.context import ResearchContext
from trader_engine.research.model_comparison import StateModelComparisonRunner
from trader_engine.research.parameters import apply_parameter_overrides
from trader_engine.research.walk_forward import WalkForwardRunner

ABLATION_PROFILES = [
    ("no_gates", []),
    ("state_only", ["state"]),
    ("transition_only", ["transition"]),
    ("consistency_only", ["consistency"]),
    ("state_transition", ["state", "transition"]),
    ("state_consistency", ["state", "consistency"]),
    ("transition_consistency", ["transition", "consistency"]),
    ("all_gates", ["state", "transition", "consistency"]),
]


@dataclass
class GateAblationResult:
    summary: pd.DataFrame
    fold_metrics: pd.DataFrame
    combo_outputs: dict[str, dict[str, pd.DataFrame]]


class GateAblationRunner:
    def __init__(self, context: ResearchContext) -> None:
        self.context = context
        self.config = context.config
        self.ablation_config = context.config.research.gate_ablation
        self.model_items = {
            item.name: item for item in context.config.research.state_model_comparison.models
        }

    def run(
        self,
        members_by_symbol: dict[str, object],
        raw_by_symbol: dict[str, pd.DataFrame],
    ) -> GateAblationResult:
        summary_records: list[dict[str, object]] = []
        fold_metric_frames: list[pd.DataFrame] = []
        combo_outputs: dict[str, dict[str, pd.DataFrame]] = {}

        for model_name in self._target_model_names():
            model_item = self.model_items.get(model_name)
            if model_item is None:
                continue
            for ablation_name, components in ABLATION_PROFILES:
                overrides = {
                    **model_item.overrides,
                    "signals.quality_gate_components": components,
                }
                if model_item.selection_policy:
                    overrides["signals.selection_policy_name"] = model_item.selection_policy
                if self.ablation_config.selection_mode is not None:
                    overrides["signals.selection_mode"] = self.ablation_config.selection_mode
                candidate_config = apply_parameter_overrides(self.config, overrides)
                candidate_context = ResearchContext(candidate_config)
                dataset = candidate_context.prepare_dataset(
                    members_by_symbol=members_by_symbol,
                    raw_by_symbol=raw_by_symbol,
                )
                walk_forward = WalkForwardRunner(candidate_context).run(dataset)
                combo_name = f"{model_name}__{ablation_name}"
                if not walk_forward.fold_metrics.empty:
                    fold_metric_frames.append(
                        walk_forward.fold_metrics.assign(
                            model_name=model_name,
                            ablation_name=ablation_name,
                            combo_name=combo_name,
                        )
                    )
                combo_outputs[combo_name] = {
                    "aggregate_metrics": walk_forward.aggregate_metrics,
                    **walk_forward.quality_frames,
                }
                summary_records.append(
                    self._summary_record(
                        model_name=model_name,
                        ablation_name=ablation_name,
                        aggregate_metrics=walk_forward.aggregate_metrics,
                    )
                )

        summary = pd.DataFrame(summary_records)
        if not summary.empty:
            summary = self._score_summary(summary)
        fold_metrics = pd.concat(fold_metric_frames, ignore_index=True) if fold_metric_frames else pd.DataFrame()
        return GateAblationResult(summary=summary, fold_metrics=fold_metrics, combo_outputs=combo_outputs)

    def _target_model_names(self) -> list[str]:
        if self.ablation_config.model_names:
            return self.ablation_config.model_names
        return list(self.model_items)

    @staticmethod
    def _summary_record(
        model_name: str,
        ablation_name: str,
        aggregate_metrics: pd.DataFrame,
    ) -> dict[str, object]:
        record = {
            "model_name": model_name,
            "ablation_name": ablation_name,
            "combo_name": f"{model_name}__{ablation_name}",
        }
        record.update(StateModelComparisonRunner._flatten_aggregate_metrics(aggregate_metrics))
        return record

    @staticmethod
    def _score_summary(summary: pd.DataFrame) -> pd.DataFrame:
        ranking_weights = {
            "mean_test_sharpe": 0.40,
            "mean_test_total_return": 0.25,
            "mean_test_expectancy": 0.20,
            "mean_test_trade_count": 0.15,
        }
        scored = summary.copy()
        metric_columns = [column for column in ranking_weights if column in scored.columns]
        scored["ablation_score"] = StateModelComparisonRunner._blended_score(scored, metric_columns, ranking_weights)
        scored = scored.sort_values(
            ["ablation_score", "mean_test_sharpe", "mean_test_total_return"],
            ascending=[False, False, False],
            na_position="last",
        ).reset_index(drop=True)
        scored.insert(0, "comparison_rank", range(1, len(scored) + 1))
        return scored
