from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from trader_engine.research.context import PreparedDataset, ResearchContext
from trader_engine.research.parameters import apply_parameter_overrides
from trader_engine.research.walk_forward import WalkForwardRunner
from trader_engine.states.diagnostics import compute_state_diagnostics


@dataclass
class StateModelComparisonResult:
    summary: pd.DataFrame
    fold_metrics: pd.DataFrame
    sensitivity_summary: pd.DataFrame
    model_outputs: dict[str, dict[str, pd.DataFrame]]


class StateModelComparisonRunner:
    def __init__(self, context: ResearchContext) -> None:
        self.context = context
        self.config = context.config
        self.comparison_config = context.config.research.state_model_comparison

    def run(
        self,
        members_by_symbol: dict[str, object],
        raw_by_symbol: dict[str, pd.DataFrame],
    ) -> StateModelComparisonResult:
        summary_records: list[dict[str, object]] = []
        fold_metric_frames: list[pd.DataFrame] = []
        sensitivity_records: list[dict[str, object]] = []
        model_outputs: dict[str, dict[str, pd.DataFrame]] = {}

        for item in self.comparison_config.models:
            candidate_overrides = dict(item.overrides)
            if item.selection_policy:
                candidate_overrides["signals.selection_policy_name"] = item.selection_policy
            candidate_config = apply_parameter_overrides(self.config, candidate_overrides)
            candidate_context = ResearchContext(candidate_config)
            dataset = candidate_context.prepare_dataset(
                members_by_symbol=members_by_symbol,
                raw_by_symbol=raw_by_symbol,
            )
            walk_forward = WalkForwardRunner(candidate_context).run(dataset)
            diagnostics = self._full_sample_diagnostics(candidate_context, dataset, walk_forward.trades)

            if not walk_forward.fold_metrics.empty:
                fold_metric_frames.append(walk_forward.fold_metrics.assign(model_name=item.name))

            model_outputs[item.name] = {
                "aggregate_metrics": walk_forward.aggregate_metrics,
                **diagnostics,
                **walk_forward.quality_frames,
            }

            sensitivity_summary = self._local_sensitivity(
                item.name,
                selection_policy=item.selection_policy,
                base_overrides=item.overrides,
                sensitivity_overrides=item.local_sensitivity_overrides,
                members_by_symbol=members_by_symbol,
                raw_by_symbol=raw_by_symbol,
            )
            if not sensitivity_summary.empty:
                sensitivity_records.extend(sensitivity_summary.to_dict(orient="records"))

            summary_records.append(
                self._summary_record(
                    model_name=item.name,
                    description=item.description,
                    selection_policy=item.selection_policy or candidate_config.signals.selection_policy_name,
                    selection_mode=candidate_config.signals.selection_mode,
                    aggregate_metrics=walk_forward.aggregate_metrics,
                    state_summary=diagnostics["state_symbol_summary"],
                    sensitivity_summary=sensitivity_summary,
                    processed_symbols=dataset.processed_symbols,
                )
            )

        summary = pd.DataFrame(summary_records)
        if not summary.empty:
            summary = self._score_summary(summary)
        fold_metrics = pd.concat(fold_metric_frames, ignore_index=True) if fold_metric_frames else pd.DataFrame()
        sensitivity = pd.DataFrame(sensitivity_records)
        return StateModelComparisonResult(
            summary=summary,
            fold_metrics=fold_metrics,
            sensitivity_summary=sensitivity,
            model_outputs=model_outputs,
        )

    def _full_sample_diagnostics(
        self,
        context: ResearchContext,
        dataset: PreparedDataset,
        trades: pd.DataFrame,
    ) -> dict[str, pd.DataFrame]:
        symbol_rows: list[dict[str, object]] = []
        state_frequency_frames: list[pd.DataFrame] = []
        persistence_frames: list[pd.DataFrame] = []
        state_quality_frames: list[pd.DataFrame] = []
        transition_quality_frames: list[pd.DataFrame] = []
        state_tradability_frames: list[pd.DataFrame] = []
        latest_states: list[dict[str, object]] = []
        artifact_frames: dict[str, list[pd.DataFrame]] = {}

        for symbol, classified in dataset.classified_by_symbol.items():
            member = dataset.members_by_symbol[symbol]
            bundle = context.bundle_for(member.asset_class)
            analysis = bundle.markov_analyzer.analyze(classified)
            symbol_trades = trades.loc[trades["symbol"] == symbol].copy() if not trades.empty and "symbol" in trades.columns else pd.DataFrame()
            if not symbol_trades.empty and "split" in symbol_trades.columns:
                symbol_trades = symbol_trades.loc[symbol_trades["split"] == "test"].copy()
            diagnostics = compute_state_diagnostics(
                classified,
                analysis,
                sparse_threshold=bundle.resolved_config.states.sparse_state_warning_threshold,
                trades=symbol_trades,
            )
            latest_state = classified.dropna(subset=["state"]).iloc[-1]["state"]
            symbol_rows.append(
                {
                    "symbol": symbol,
                    "asset_class": member.asset_class.value,
                    "current_state": latest_state,
                    **diagnostics.summary,
                }
            )
            latest_states.append({"symbol": symbol, "asset_class": member.asset_class.value, "current_state": latest_state})
            state_frequency_frames.append(diagnostics.state_frequency.assign(symbol=symbol, asset_class=member.asset_class.value))
            persistence_frames.append(diagnostics.state_persistence.assign(symbol=symbol, asset_class=member.asset_class.value))
            state_quality_frames.append(diagnostics.state_quality.assign(symbol=symbol, asset_class=member.asset_class.value))
            transition_quality_frames.append(
                diagnostics.transition_quality.assign(symbol=symbol, asset_class=member.asset_class.value)
            )
            state_tradability_frames.append(
                diagnostics.state_tradability.assign(symbol=symbol, asset_class=member.asset_class.value)
            )
            for artifact_name, artifact in dataset.state_model_artifacts_by_symbol.get(symbol, {}).items():
                if isinstance(artifact, pd.DataFrame):
                    artifact_frames.setdefault(artifact_name, []).append(
                        artifact.assign(symbol=symbol, asset_class=member.asset_class.value)
                    )

        state_symbol_summary = pd.DataFrame(symbol_rows)
        current_state_distribution = (
            pd.DataFrame(latest_states).groupby(["asset_class", "current_state"]).size().reset_index(name="count")
            if latest_states
            else pd.DataFrame()
        )
        outputs = {
            "state_symbol_summary": state_symbol_summary,
            "state_frequency": pd.concat(state_frequency_frames, ignore_index=True) if state_frequency_frames else pd.DataFrame(),
            "state_persistence": pd.concat(persistence_frames, ignore_index=True) if persistence_frames else pd.DataFrame(),
            "state_quality": pd.concat(state_quality_frames, ignore_index=True) if state_quality_frames else pd.DataFrame(),
            "transition_quality": (
                pd.concat(transition_quality_frames, ignore_index=True) if transition_quality_frames else pd.DataFrame()
            ),
            "state_tradability": (
                pd.concat(state_tradability_frames, ignore_index=True) if state_tradability_frames else pd.DataFrame()
            ),
            "current_state_distribution": current_state_distribution,
        }
        for artifact_name, frames in artifact_frames.items():
            outputs[artifact_name] = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        return outputs

    def _local_sensitivity(
        self,
        model_name: str,
        selection_policy: str | None,
        base_overrides: dict[str, Any],
        sensitivity_overrides: list[dict[str, Any]],
        members_by_symbol: dict[str, object],
        raw_by_symbol: dict[str, pd.DataFrame],
    ) -> pd.DataFrame:
        if not sensitivity_overrides:
            return pd.DataFrame()

        rows = []
        for variant_number, override in enumerate(sensitivity_overrides, start=1):
            merged_overrides = {**base_overrides, **override}
            if selection_policy:
                merged_overrides["signals.selection_policy_name"] = selection_policy
            candidate_config = apply_parameter_overrides(self.config, merged_overrides)
            candidate_context = ResearchContext(candidate_config)
            dataset = candidate_context.prepare_dataset(
                members_by_symbol=members_by_symbol,
                raw_by_symbol=raw_by_symbol,
            )
            aggregate = WalkForwardRunner(candidate_context).run(dataset).aggregate_metrics
            rows.append(
                {
                    "model_name": model_name,
                    "variant_id": f"{model_name}_variant_{variant_number}",
                    "overrides": str(override),
                    **self._flatten_aggregate_metrics(aggregate),
                }
            )
        return pd.DataFrame(rows)

    @staticmethod
    def _summary_record(
        model_name: str,
        description: str | None,
        selection_policy: str | None,
        selection_mode: str,
        aggregate_metrics: pd.DataFrame,
        state_summary: pd.DataFrame,
        sensitivity_summary: pd.DataFrame,
        processed_symbols: int,
    ) -> dict[str, object]:
        record: dict[str, object] = {
            "model_name": model_name,
            "description": description or "",
            "selection_policy": selection_policy or "",
            "selection_mode": selection_mode,
            "processed_symbols": processed_symbols,
        }
        record.update(StateModelComparisonRunner._flatten_aggregate_metrics(aggregate_metrics))
        record["mean_unique_states"] = float(state_summary["unique_states"].mean()) if not state_summary.empty else 0.0
        record["mean_median_state_observations"] = (
            float(state_summary["median_state_observations"].mean()) if not state_summary.empty else 0.0
        )
        record["min_active_state_observations"] = (
            float(state_summary["min_state_observations"].min()) if not state_summary.empty else 0.0
        )
        record["share_states_above_threshold"] = (
            float(state_summary["share_states_above_threshold"].mean()) if not state_summary.empty else 0.0
        )
        record["state_coverage"] = (
            float(1.0 - state_summary["sparse_state_ratio"].mean()) if not state_summary.empty else 0.0
        )
        record["effective_state_coverage"] = (
            float(state_summary["effective_state_coverage"].mean()) if not state_summary.empty else 0.0
        )
        record["transition_stability"] = (
            float(state_summary["transition_stability"].mean()) if not state_summary.empty else 0.0
        )
        record["transition_concentration"] = (
            float(state_summary["transition_concentration"].mean()) if not state_summary.empty else 0.0
        )
        record["mean_state_persistence"] = (
            float(state_summary["mean_state_persistence"].mean()) if not state_summary.empty else 0.0
        )
        record["trade_generating_state_percentage"] = (
            float(state_summary["trade_generating_state_percentage"].mean()) if not state_summary.empty else 0.0
        )
        record["total_out_of_sample_trade_count"] = (
            float(state_summary["out_of_sample_trade_count"].sum()) if not state_summary.empty else 0.0
        )
        if not sensitivity_summary.empty:
            record["parameter_sensitivity_sharpe_std"] = float(sensitivity_summary["mean_test_sharpe"].std())
            record["parameter_sensitivity_return_std"] = float(sensitivity_summary["mean_test_total_return"].std())
        else:
            record["parameter_sensitivity_sharpe_std"] = 0.0
            record["parameter_sensitivity_return_std"] = 0.0
        return record

    def _score_summary(self, summary: pd.DataFrame) -> pd.DataFrame:
        scored = summary.copy()
        ranking_weights = self.comparison_config.ranking_weights

        sparsity_components = [
            column
            for column in ["effective_state_coverage", "share_states_above_threshold", "mean_median_state_observations"]
            if column in scored.columns
        ]
        scored["sparsity_score"] = self._blended_score(scored, sparsity_components)

        tradability_components = [column for column in ranking_weights if column in scored.columns]
        scored["tradability_score"] = self._blended_score(scored, tradability_components, ranking_weights)
        scored = scored.sort_values(
            ["tradability_score", "mean_test_sharpe", "mean_test_total_return"],
            ascending=[False, False, False],
            na_position="last",
        ).reset_index(drop=True)
        scored.insert(0, "comparison_rank", range(1, len(scored) + 1))
        return scored

    @staticmethod
    def _blended_score(
        frame: pd.DataFrame,
        columns: list[str],
        weights: dict[str, float] | None = None,
    ) -> pd.Series:
        if not columns:
            return pd.Series(0.0, index=frame.index)
        normalized_columns = []
        weight_sum = 0.0
        for column in columns:
            series = frame[column].astype(float).fillna(0.0)
            minimum = float(series.min())
            maximum = float(series.max())
            if maximum == minimum:
                normalized = pd.Series(0.5, index=frame.index)
            else:
                normalized = (series - minimum) / (maximum - minimum)
            weight = float(weights[column]) if weights and column in weights else 1.0
            weight_sum += weight
            normalized_columns.append(normalized * weight)
        if weight_sum == 0.0:
            return pd.Series(0.0, index=frame.index)
        return sum(normalized_columns) / weight_sum

    @staticmethod
    def _flatten_aggregate_metrics(aggregate_metrics: pd.DataFrame) -> dict[str, float]:
        payload: dict[str, float] = {}
        if aggregate_metrics.empty:
            return payload
        for _, row in aggregate_metrics.iterrows():
            split = str(row["split"])
            payload[f"mean_{split}_total_return"] = float(row["mean_total_return"])
            payload[f"mean_{split}_sharpe"] = float(row["mean_sharpe"])
            payload[f"mean_{split}_max_drawdown"] = float(row["mean_max_drawdown"])
            payload[f"mean_{split}_trade_count"] = float(row["mean_trade_count"])
            payload[f"mean_{split}_expectancy"] = float(row["mean_expectancy"])
            if "mean_average_win" in row:
                payload[f"mean_{split}_average_win"] = float(row["mean_average_win"])
            if "mean_average_loss" in row:
                payload[f"mean_{split}_average_loss"] = float(row["mean_average_loss"])
            if "mean_win_rate" in row:
                payload[f"mean_{split}_win_rate"] = float(row["mean_win_rate"])
            payload[f"positive_fold_rate_{split}"] = float(row["positive_fold_rate"])
        return payload
