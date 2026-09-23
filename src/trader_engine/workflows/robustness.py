from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from trader_engine.analytics.export import ArtifactStore
from trader_engine.core.models import AssetClass
from trader_engine.research.context import ResearchContext
from trader_engine.research.exit_comparison import ExitComparisonRunner
from trader_engine.research.gate_ablation import GateAblationRunner
from trader_engine.research.model_comparison import StateModelComparisonRunner
from trader_engine.research.policy_comparison import SelectionPolicyComparisonRunner
from trader_engine.research.sweeps import ParameterSweepRunner
from trader_engine.research.walk_forward import WalkForwardRunner


class RobustnessWorkflow:
    def __init__(self, config) -> None:
        self.config = config
        self.context = ResearchContext(config)
        self.artifacts = ArtifactStore(config.storage.output_path())

    def run_walk_forward(self) -> dict[str, object]:
        dataset = self.context.prepare_dataset()
        runner = WalkForwardRunner(self.context)
        result = runner.run(dataset)

        self._write_walk_forward_artifacts("research/walk_forward", result)

        metadata = {
            "run_at": datetime.now(timezone.utc).isoformat(),
            "processed_symbols": dataset.processed_symbols,
            "skipped_symbols": dataset.skipped_symbols,
            "fold_count": int(result.folds["fold_id"].nunique()) if not result.folds.empty else 0,
        }
        self.artifacts.write_json("research/walk_forward/summary.json", metadata)
        return {"metadata": metadata, "aggregate_metrics": result.aggregate_metrics}

    def run_walk_forward_isolated(self) -> dict[str, object]:
        members_by_symbol, raw_by_symbol, counters = self.context.load_raw_market_data()
        results: dict[str, object] = {}

        for asset_class in (AssetClass.EQUITY, AssetClass.CRYPTO):
            filtered_members = {
                symbol: member
                for symbol, member in members_by_symbol.items()
                if member.asset_class == asset_class
            }
            filtered_raw = {
                symbol: raw_by_symbol[symbol]
                for symbol in filtered_members
                if symbol in raw_by_symbol
            }
            dataset = self.context.prepare_dataset(filtered_members, filtered_raw)
            runner = WalkForwardRunner(self.context)
            result = runner.run(dataset)
            base_path = f"research/walk_forward_isolated/{asset_class.value}"
            self._write_walk_forward_artifacts(base_path, result)
            metadata = {
                "run_at": datetime.now(timezone.utc).isoformat(),
                "asset_class": asset_class.value,
                "processed_symbols": dataset.processed_symbols,
                "skipped_symbols": dataset.skipped_symbols,
                "fold_count": int(result.folds["fold_id"].nunique()) if not result.folds.empty else 0,
            }
            self.artifacts.write_json(f"{base_path}/summary.json", metadata)
            results[asset_class.value] = {
                "metadata": metadata,
                "aggregate_metrics": result.aggregate_metrics,
            }

        self.artifacts.write_json(
            "research/walk_forward_isolated/summary.json",
            {
                "run_at": datetime.now(timezone.utc).isoformat(),
                "processed_symbols": counters["processed_symbols"],
                "skipped_symbols": counters["skipped_symbols"],
                "asset_classes": list(results),
            },
        )
        return results

    def run_parameter_sweep(self) -> dict[str, object]:
        members_by_symbol, raw_by_symbol, counters = self.context.load_raw_market_data()
        runner = ParameterSweepRunner(self.context)
        result = runner.run(members_by_symbol=members_by_symbol, raw_by_symbol=raw_by_symbol)

        self.artifacts.write_frame("research/parameter_sweep/summary.csv", result.summary)
        self.artifacts.write_frame("research/parameter_sweep/fold_metrics.csv", result.fold_metrics)
        self.artifacts.write_json(
            "research/parameter_sweep/parameter_definitions.json",
            {"parameter_definitions": result.parameter_definitions},
        )
        self.artifacts.write_json(
            "research/parameter_sweep/summary.json",
            {
                "run_at": datetime.now(timezone.utc).isoformat(),
                "processed_symbols": counters["processed_symbols"],
                "skipped_symbols": counters["skipped_symbols"],
                "parameter_count": len(result.parameter_definitions),
            },
        )
        return {
            "parameter_count": len(result.parameter_definitions),
            "summary": result.summary,
        }

    def run_state_model_comparison(self) -> dict[str, object]:
        members_by_symbol, raw_by_symbol, counters = self.context.load_raw_market_data()
        runner = StateModelComparisonRunner(self.context)
        result = runner.run(members_by_symbol=members_by_symbol, raw_by_symbol=raw_by_symbol)

        self.artifacts.write_frame("research/state_model_comparison/summary.csv", result.summary)
        self.artifacts.write_frame("research/state_model_comparison/fold_metrics.csv", result.fold_metrics)
        self.artifacts.write_frame("research/state_model_comparison/sensitivity_summary.csv", result.sensitivity_summary)
        for model_name, outputs in result.model_outputs.items():
            for artifact_name, frame in outputs.items():
                self.artifacts.write_frame(
                    f"research/state_model_comparison/{model_name}/{artifact_name}.csv",
                    frame,
                )
        self.artifacts.write_json(
            "research/state_model_comparison/summary.json",
            {
                "run_at": datetime.now(timezone.utc).isoformat(),
                "processed_symbols": counters["processed_symbols"],
                "skipped_symbols": counters["skipped_symbols"],
                "model_count": int(result.summary["model_name"].nunique()) if not result.summary.empty else 0,
            },
        )
        return {"summary": result.summary, "model_outputs": result.model_outputs}

    def run_exit_comparison(self) -> dict[str, object]:
        members_by_symbol, raw_by_symbol, counters = self.context.load_raw_market_data()
        runner = ExitComparisonRunner(self.context)
        result = runner.run(members_by_symbol=members_by_symbol, raw_by_symbol=raw_by_symbol)

        self.artifacts.write_frame("research/exit_comparison/summary.csv", result.summary)
        self.artifacts.write_frame("research/exit_comparison/fold_metrics.csv", result.fold_metrics)
        for combo_name, outputs in result.combo_outputs.items():
            for artifact_name, frame in outputs.items():
                self.artifacts.write_frame(
                    f"research/exit_comparison/{combo_name}/{artifact_name}.csv",
                    frame,
                )
        self.artifacts.write_json(
            "research/exit_comparison/summary.json",
            {
                "run_at": datetime.now(timezone.utc).isoformat(),
                "processed_symbols": counters["processed_symbols"],
                "skipped_symbols": counters["skipped_symbols"],
                "combo_count": int(result.summary["combo_name"].nunique()) if not result.summary.empty else 0,
            },
        )
        return {"summary": result.summary, "combo_outputs": result.combo_outputs}

    def run_selection_policy_comparison(self) -> dict[str, object]:
        members_by_symbol, raw_by_symbol, counters = self.context.load_raw_market_data()
        runner = SelectionPolicyComparisonRunner(self.context)
        result = runner.run(members_by_symbol=members_by_symbol, raw_by_symbol=raw_by_symbol)

        self.artifacts.write_frame("research/selection_policy_comparison/summary.csv", result.summary)
        self.artifacts.write_frame("research/selection_policy_comparison/fold_metrics.csv", result.fold_metrics)
        for experiment_name, outputs in result.experiment_outputs.items():
            for artifact_name, frame in outputs.items():
                self.artifacts.write_frame(
                    f"research/selection_policy_comparison/{experiment_name}/{artifact_name}.csv",
                    frame,
                )
        self.artifacts.write_json(
            "research/selection_policy_comparison/summary.json",
            {
                "run_at": datetime.now(timezone.utc).isoformat(),
                "processed_symbols": counters["processed_symbols"],
                "skipped_symbols": counters["skipped_symbols"],
                "experiment_count": int(result.summary["experiment_name"].nunique()) if not result.summary.empty else 0,
            },
        )
        return {"summary": result.summary, "experiment_outputs": result.experiment_outputs}

    def run_gate_ablation(self) -> dict[str, object]:
        members_by_symbol, raw_by_symbol, counters = self.context.load_raw_market_data()
        runner = GateAblationRunner(self.context)
        result = runner.run(members_by_symbol=members_by_symbol, raw_by_symbol=raw_by_symbol)

        self.artifacts.write_frame("research/gate_ablation/summary.csv", result.summary)
        self.artifacts.write_frame("research/gate_ablation/fold_metrics.csv", result.fold_metrics)
        for combo_name, outputs in result.combo_outputs.items():
            for artifact_name, frame in outputs.items():
                self.artifacts.write_frame(
                    f"research/gate_ablation/{combo_name}/{artifact_name}.csv",
                    frame,
                )
        self.artifacts.write_json(
            "research/gate_ablation/summary.json",
            {
                "run_at": datetime.now(timezone.utc).isoformat(),
                "processed_symbols": counters["processed_symbols"],
                "skipped_symbols": counters["skipped_symbols"],
                "combo_count": int(result.summary["combo_name"].nunique()) if not result.summary.empty else 0,
            },
        )
        return {"summary": result.summary, "combo_outputs": result.combo_outputs}

    def _write_walk_forward_artifacts(self, base_path: str, result) -> None:
        self.artifacts.write_frame(f"{base_path}/folds.csv", result.folds)
        self.artifacts.write_frame(f"{base_path}/fold_metrics.csv", result.fold_metrics)
        self.artifacts.write_frame(f"{base_path}/aggregate_metrics.csv", result.aggregate_metrics)
        self.artifacts.write_frame(f"{base_path}/trades.csv", result.trades)
        self.artifacts.write_frame(f"{base_path}/orders.csv", result.orders)
        for name, frame in result.attribution_frames.items():
            self.artifacts.write_frame(f"{base_path}/attribution/{name}.csv", frame)
        for name, frame in result.diagnostic_frames.items():
            self.artifacts.write_frame(f"{base_path}/diagnostics/{name}.csv", frame)
        for name, frame in result.quality_frames.items():
            self.artifacts.write_frame(f"{base_path}/quality/{name}.csv", frame)
