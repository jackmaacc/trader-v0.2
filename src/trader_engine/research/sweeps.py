from __future__ import annotations

import json
from dataclasses import dataclass

import pandas as pd

from trader_engine.research.context import ResearchContext
from trader_engine.research.parameters import apply_parameter_overrides, generate_parameter_grid
from trader_engine.research.walk_forward import WalkForwardRunner


@dataclass
class ParameterSweepResult:
    summary: pd.DataFrame
    fold_metrics: pd.DataFrame
    parameter_definitions: list[dict[str, object]]


class ParameterSweepRunner:
    def __init__(self, context: ResearchContext) -> None:
        self.context = context
        self.config = context.config
        self.sweep_config = context.config.research.parameter_sweep

    def run(
        self,
        members_by_symbol: dict[str, object],
        raw_by_symbol: dict[str, pd.DataFrame],
    ) -> ParameterSweepResult:
        parameter_sets = generate_parameter_grid(
            self.sweep_config.search_space,
            max_combinations=self.sweep_config.max_combinations,
        )
        if not parameter_sets:
            empty = pd.DataFrame()
            return ParameterSweepResult(empty, empty, [])

        summary_records: list[dict[str, object]] = []
        fold_metric_frames: list[pd.DataFrame] = []
        definitions: list[dict[str, object]] = []

        for parameter_index, overrides in enumerate(parameter_sets, start=1):
            parameter_id = f"param_{parameter_index:04d}"
            candidate_config = apply_parameter_overrides(self.config, overrides)
            candidate_context = ResearchContext(candidate_config)
            dataset = candidate_context.prepare_dataset(
                members_by_symbol=members_by_symbol,
                raw_by_symbol=raw_by_symbol,
            )
            walk_forward = WalkForwardRunner(candidate_context)
            result = walk_forward.run(dataset)

            definitions.append({"parameter_id": parameter_id, "overrides": overrides})
            fold_metrics = result.fold_metrics.copy()
            if not fold_metrics.empty:
                fold_metrics["parameter_id"] = parameter_id
                fold_metrics["overrides"] = json.dumps(overrides, sort_keys=True)
                fold_metric_frames.append(fold_metrics)

            summary_records.append(
                self._summary_record(
                    parameter_id=parameter_id,
                    overrides=overrides,
                    aggregate_metrics=result.aggregate_metrics,
                )
            )

        summary = pd.DataFrame(summary_records)
        if not summary.empty and self.sweep_config.rank_metric in summary.columns:
            summary = summary.sort_values(
                [self.sweep_config.rank_metric, "mean_test_expectancy"],
                ascending=[False, False],
            ).reset_index(drop=True)
            summary["rank"] = range(1, len(summary) + 1)

        fold_metrics = pd.concat(fold_metric_frames, ignore_index=True) if fold_metric_frames else pd.DataFrame()
        return ParameterSweepResult(summary=summary, fold_metrics=fold_metrics, parameter_definitions=definitions)

    @staticmethod
    def _summary_record(
        parameter_id: str,
        overrides: dict[str, object],
        aggregate_metrics: pd.DataFrame,
    ) -> dict[str, object]:
        record: dict[str, object] = {
            "parameter_id": parameter_id,
            "overrides": json.dumps(overrides, sort_keys=True),
        }
        for path, value in overrides.items():
            record[f"param_{_sanitize_path(path)}"] = value
        if aggregate_metrics.empty:
            return record
        for _, row in aggregate_metrics.iterrows():
            split = str(row["split"])
            record[f"fold_count_{split}"] = int(row["fold_count"])
            record[f"mean_{split}_total_return"] = float(row["mean_total_return"])
            record[f"mean_{split}_sharpe"] = float(row["mean_sharpe"])
            record[f"mean_{split}_max_drawdown"] = float(row["mean_max_drawdown"])
            record[f"mean_{split}_trade_count"] = float(row["mean_trade_count"])
            record[f"mean_{split}_expectancy"] = float(row["mean_expectancy"])
            record[f"positive_fold_rate_{split}"] = float(row["positive_fold_rate"])
        return record


def _sanitize_path(path: str) -> str:
    return path.replace(".", "__").replace("-", "_")
