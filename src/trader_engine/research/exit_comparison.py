from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from time import perf_counter

import pandas as pd

from trader_engine.research.context import ResearchContext
from trader_engine.research.model_comparison import StateModelComparisonRunner
from trader_engine.research.parameters import apply_parameter_overrides
from trader_engine.research.walk_forward import WalkForwardRunner


@dataclass
class ExitComparisonResult:
    summary: pd.DataFrame
    fold_metrics: pd.DataFrame
    combo_outputs: dict[str, dict[str, pd.DataFrame]]


class ExitComparisonRunner:
    def __init__(self, context: ResearchContext) -> None:
        self.context = context
        self.config = context.config
        self.exit_config = context.config.research.exit_comparison
        self.model_items = {
            item.name: item for item in context.config.research.state_model_comparison.models
        }

    def run(
        self,
        members_by_symbol: dict[str, object],
        raw_by_symbol: dict[str, pd.DataFrame],
    ) -> ExitComparisonResult:
        summary_records: list[dict[str, object]] = []
        fold_metric_frames: list[pd.DataFrame] = []
        combo_outputs: dict[str, dict[str, pd.DataFrame]] = {}

        for model_name in self._target_model_names():
            model_item = self.model_items.get(model_name)
            if model_item is None:
                continue
            model_overrides = dict(model_item.overrides)
            if model_item.selection_policy:
                model_overrides["signals.selection_policy_name"] = model_item.selection_policy
            model_config = apply_parameter_overrides(self.config, model_overrides)
            model_context = ResearchContext(model_config)
            prepare_start = perf_counter()
            dataset = model_context.prepare_dataset(
                members_by_symbol=members_by_symbol,
                raw_by_symbol=raw_by_symbol,
            )
            runner = WalkForwardRunner(model_context)
            prepared = runner.prepare(dataset)
            prepare_seconds = perf_counter() - prepare_start
            for asset_class in {member.asset_class for member in dataset.members_by_symbol.values()}:
                model_context.bundle_for(asset_class)

            profile_runs = self._evaluate_profiles(
                model_name=model_name,
                model_item=model_item,
                model_config=model_config,
                model_context=model_context,
                dataset=dataset,
                runner=runner,
                prepared=prepared,
                prepare_seconds=prepare_seconds,
            )
            for profile_name, walk_forward, runtime_seconds in profile_runs:
                combo_name = f"{model_name}__{profile_name}"
                if not walk_forward.fold_metrics.empty:
                    fold_metric_frames.append(
                        walk_forward.fold_metrics.assign(
                            model_name=model_name,
                            exit_profile=profile_name,
                            combo_name=combo_name,
                        )
                    )
                combo_outputs[combo_name] = {
                    "aggregate_metrics": walk_forward.aggregate_metrics,
                    "runtime_summary": pd.DataFrame(
                        [
                            {
                                "model_name": model_name,
                                "exit_profile": profile_name,
                                "prepare_seconds": prepare_seconds,
                                "run_seconds": runtime_seconds,
                                "total_seconds": prepare_seconds + runtime_seconds,
                                "prepared_cache_reused": True,
                                "fold_count": int(prepared.folds_frame["fold_id"].nunique()) if not prepared.folds_frame.empty else 0,
                            }
                        ]
                    ),
                    **walk_forward.attribution_frames,
                    **walk_forward.diagnostic_frames,
                    **walk_forward.quality_frames,
                }
                summary_records.append(
                    self._summary_record(
                        model_name=model_name,
                        profile_name=profile_name,
                        model_description=model_item.description,
                        profile_description=self._profile_description(profile_name),
                        aggregate_metrics=walk_forward.aggregate_metrics,
                        trades=walk_forward.trades,
                        quality_frames=walk_forward.quality_frames,
                        processed_symbols=dataset.processed_symbols,
                        prepare_seconds=prepare_seconds,
                        run_seconds=runtime_seconds,
                    )
                )

        summary = pd.DataFrame(summary_records)
        if not summary.empty:
            summary = self._score_summary(summary)
        fold_metrics = pd.concat(fold_metric_frames, ignore_index=True) if fold_metric_frames else pd.DataFrame()
        return ExitComparisonResult(
            summary=summary,
            fold_metrics=fold_metrics,
            combo_outputs=combo_outputs,
        )

    def _target_model_names(self) -> list[str]:
        if self.exit_config.model_names:
            return self.exit_config.model_names
        return list(self.model_items)

    @staticmethod
    def _summary_record(
        model_name: str,
        profile_name: str,
        model_description: str | None,
        profile_description: str | None,
        aggregate_metrics: pd.DataFrame,
        trades: pd.DataFrame,
        quality_frames: dict[str, pd.DataFrame],
        processed_symbols: int,
        prepare_seconds: float,
        run_seconds: float,
    ) -> dict[str, object]:
        state_quality = quality_frames.get("test_state_quality_summary", pd.DataFrame())
        transition_quality = quality_frames.get("test_transition_quality_summary", pd.DataFrame())
        family_performance = quality_frames.get("test_state_family_performance", pd.DataFrame())

        record: dict[str, object] = {
            "model_name": model_name,
            "exit_profile": profile_name,
            "combo_name": f"{model_name}__{profile_name}",
            "model_description": model_description or "",
            "profile_description": profile_description or "",
            "processed_symbols": processed_symbols,
            "prepare_seconds": prepare_seconds,
            "run_seconds": run_seconds,
            "total_runtime_seconds": prepare_seconds + run_seconds,
            "prepared_cache_reused": True,
            "test_state_count": int(state_quality["state"].nunique()) if "state" in state_quality.columns else 0,
            "positive_state_count": int((state_quality.get("expectancy", pd.Series(dtype=float)) > 0).sum()),
            "positive_transition_count": int(
                (transition_quality.get("expectancy", pd.Series(dtype=float)) > 0).sum()
            ),
            "state_family_count": (
                int(family_performance["entry_state_family"].nunique())
                if "entry_state_family" in family_performance.columns
                else 0
            ),
        }
        record.update(StateModelComparisonRunner._flatten_aggregate_metrics(aggregate_metrics))
        record.update(ExitComparisonRunner._test_trade_summary(trades))
        return record

    @staticmethod
    def _score_summary(summary: pd.DataFrame) -> pd.DataFrame:
        scored = summary.copy()
        scored["drawdown_control"] = -scored.get("mean_test_max_drawdown", pd.Series(0.0, index=scored.index))
        scored["loss_control"] = -scored.get("test_average_loss_magnitude", pd.Series(0.0, index=scored.index))
        scored["capture_efficiency"] = -scored.get(
            "test_average_favorable_excursion_left_uncaptured_pct",
            pd.Series(0.0, index=scored.index),
        )
        ranked_weights = {
            "mean_test_expectancy": 0.25,
            "test_win_loss_size_ratio": 0.25,
            "loss_control": 0.15,
            "capture_efficiency": 0.15,
            "mean_test_sharpe": 0.10,
            "drawdown_control": 0.10,
        }
        metric_columns = [column for column in ranked_weights if column in scored.columns]
        scored["payoff_asymmetry_score"] = StateModelComparisonRunner._blended_score(
            scored,
            metric_columns,
            ranked_weights,
        )
        scored["exit_score"] = scored["payoff_asymmetry_score"]
        scored = scored.sort_values(
            ["payoff_asymmetry_score", "mean_test_expectancy", "test_win_loss_size_ratio"],
            ascending=[False, False, False],
            na_position="last",
        ).reset_index(drop=True)
        scored.insert(0, "comparison_rank", range(1, len(scored) + 1))
        return scored

    def _evaluate_profiles(
        self,
        model_name: str,
        model_item,
        model_config,
        model_context: ResearchContext,
        dataset,
        runner: WalkForwardRunner,
        prepared,
        prepare_seconds: float,
    ) -> list[tuple[str, object, float]]:
        profiles = list(self.exit_config.profiles)
        if self.exit_config.max_workers <= 1 or len(profiles) <= 1:
            return [
                self._run_profile(model_name, model_item, model_config, runner, prepared, profile)
                for profile in profiles
            ]

        results: list[tuple[str, object, float]] = []
        with ThreadPoolExecutor(max_workers=self.exit_config.max_workers) as executor:
            futures = {
                executor.submit(
                    self._run_profile,
                    model_name,
                    model_item,
                    model_config,
                    runner,
                    prepared,
                    profile,
                ): profile.name
                for profile in profiles
            }
            for future in as_completed(futures):
                results.append(future.result())
        return sorted(results, key=lambda item: item[0])

    def _run_profile(
        self,
        model_name: str,
        model_item,
        model_config,
        runner: WalkForwardRunner,
        prepared,
        profile,
    ) -> tuple[str, object, float]:
        combined_overrides = {**model_item.overrides, **profile.overrides}
        if model_item.selection_policy:
            combined_overrides["signals.selection_policy_name"] = model_item.selection_policy
        candidate_config = apply_parameter_overrides(self.config, combined_overrides)
        profile_context = ResearchContext(candidate_config)
        runtime_start = perf_counter()
        walk_forward = runner.run_prepared(prepared, backtest_engine=profile_context.build_backtest_engine())
        runtime_seconds = perf_counter() - runtime_start
        return profile.name, walk_forward, runtime_seconds

    def _profile_description(self, profile_name: str) -> str | None:
        for profile in self.exit_config.profiles:
            if profile.name == profile_name:
                return profile.description
        return None

    @staticmethod
    def _test_trade_summary(trades: pd.DataFrame) -> dict[str, float]:
        if trades.empty or "split" not in trades.columns:
            return {}
        test_trades = trades.loc[trades["split"] == "test"].copy()
        if test_trades.empty:
            return {}

        returns = pd.to_numeric(test_trades.get("return_pct"), errors="coerce").fillna(0.0)
        wins = returns.loc[returns > 0]
        losses = returns.loc[returns <= 0]
        average_win = float(wins.mean()) if not wins.empty else 0.0
        average_loss = float(losses.mean()) if not losses.empty else 0.0
        loss_magnitude = abs(average_loss)
        return {
            "test_trade_count_total": float(len(test_trades)),
            "test_win_rate": float((returns > 0).mean()),
            "test_average_win": average_win,
            "test_average_loss": average_loss,
            "test_average_loss_magnitude": loss_magnitude,
            "test_win_loss_size_ratio": average_win / loss_magnitude if loss_magnitude > 0 else 0.0,
            "test_average_mae_pct": float(pd.to_numeric(test_trades.get("mae_pct"), errors="coerce").mean()),
            "test_average_mfe_pct": float(pd.to_numeric(test_trades.get("mfe_pct"), errors="coerce").mean()),
            "test_average_favorable_excursion_left_uncaptured_pct": float(
                pd.to_numeric(
                    test_trades.get("favorable_excursion_left_uncaptured_pct"),
                    errors="coerce",
                ).mean()
            ),
        }
