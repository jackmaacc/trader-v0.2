from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd

from trader_engine.analytics.diagnostics import (
    performance_by_asset_class,
    performance_by_volatility_regime,
    state_edge_summary,
    trade_diagnostics,
    transition_edge_summary,
)
from trader_engine.analytics.export import ArtifactStore
from trader_engine.core.config import AppConfig
from trader_engine.research.context import ResearchContext
from trader_engine.research.quality import build_quality_gate_tables, quality_report_frames
from trader_engine.research.selection import annotate_opportunity_ranks, apply_opportunity_caps, selection_report_frames
from trader_engine.signals.engine import SignalEngine
from trader_engine.states.diagnostics import compute_state_diagnostics

LOGGER = logging.getLogger(__name__)


class ResearchWorkflow:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.context = ResearchContext(config)
        self.artifacts = ArtifactStore(config.storage.output_path())

    def run(self) -> dict[str, object]:
        LOGGER.info("Running research workflow")
        dataset = self.context.prepare_dataset()
        analyses_by_symbol = {}
        frames_by_symbol: dict[str, pd.DataFrame] = {}
        for symbol, member in dataset.members_by_symbol.items():
            classified = dataset.classified_by_symbol[symbol]
            bundle = self.context.bundle_for(member.asset_class)
            signal_frame = bundle.signal_engine.generate_historical_signals(
                member=member,
                frame=classified,
                analyzer=bundle.markov_analyzer,
            )
            analysis = bundle.markov_analyzer.analyze(classified)
            analyses_by_symbol[symbol] = analysis

            frames_by_symbol[member.symbol] = signal_frame.assign(
                asset_class=member.asset_class.value,
                model_name=bundle.resolved_config.states.model_name,
                selection_policy_name=bundle.resolved_config.signals.selection_policy_name,
            )

            state_diagnostics = compute_state_diagnostics(
                signal_frame,
                analysis,
                sparse_threshold=bundle.resolved_config.states.sparse_state_warning_threshold,
            )
            self._write_symbol_artifacts(
                member.symbol,
                signal_frame,
                analysis,
                state_diagnostics,
                dataset.state_model_artifacts_by_symbol.get(member.symbol, {}),
            )

        capped = apply_opportunity_caps(frames_by_symbol)
        backtest = self.context.build_backtest_engine().run(
            frames_by_symbol=capped.frames_by_symbol,
            members_by_symbol=dataset.members_by_symbol,
            initial_capital=self.config.risk.initial_capital,
            risk_history_by_symbol={s: f['close'] for s, f in dataset.raw_by_symbol.items()},
        )
        quality_tables, _ = build_quality_gate_tables(
            backtest.trades,
            min_group_observations=self.config.research.diagnostics.min_group_observations,
        )
        opportunities = []
        for symbol, member in dataset.members_by_symbol.items():
            bundle = self.context.bundle_for(member.asset_class)
            opportunity = bundle.signal_engine.opportunity_from_latest(
                member,
                dataset.classified_by_symbol[symbol],
                analyses_by_symbol[symbol],
                quality_tables=quality_tables,
            )
            opportunity.metadata["model_name"] = bundle.resolved_config.states.model_name
            opportunity.metadata["selection_policy_name"] = bundle.resolved_config.signals.selection_policy_name
            opportunities.append(opportunity)
        opportunities_frame = SignalEngine.opportunities_to_frame(opportunities) if opportunities else pd.DataFrame()
        opportunities_frame = annotate_opportunity_ranks(opportunities_frame)

        metadata = {
            "run_at": datetime.now(timezone.utc).isoformat(),
            "processed_symbols": dataset.processed_symbols,
            "skipped_symbols": dataset.skipped_symbols,
            "universe_size": len(dataset.members_by_symbol) + dataset.skipped_symbols,
            "interval": self.config.data.interval,
        }
        self._write_global_artifacts(
            opportunities_frame,
            backtest,
            metadata,
            selection_candidates=capped.candidates,
        )

        return {
            "artifacts_dir": str(self.config.storage.output_path()),
            "opportunities": opportunities_frame,
            "metrics": backtest.metrics,
            "metadata": metadata,
        }

    def _write_symbol_artifacts(self, symbol: str, frame: pd.DataFrame, analysis, state_diagnostics, model_artifacts) -> None:
        self.artifacts.write_frame(f"symbols/{symbol}.parquet", frame)
        self.artifacts.write_frame(f"markov/{symbol}_transition_matrix.csv", analysis.transition_matrix)
        self.artifacts.write_frame(f"markov/{symbol}_transition_counts.csv", analysis.transition_counts)
        self.artifacts.write_frame(f"markov/{symbol}_state_returns.csv", analysis.state_returns)
        self.artifacts.write_frame(f"markov/{symbol}_transition_returns.csv", analysis.transition_returns)
        self.artifacts.write_frame(f"markov/{symbol}_confusion_matrix.csv", analysis.confusion_matrix)
        self.artifacts.write_frame(f"state_diagnostics/{symbol}_state_frequency.csv", state_diagnostics.state_frequency)
        self.artifacts.write_frame(f"state_diagnostics/{symbol}_state_persistence.csv", state_diagnostics.state_persistence)
        self.artifacts.write_frame(f"state_diagnostics/{symbol}_state_quality.csv", state_diagnostics.state_quality)
        self.artifacts.write_frame(f"state_diagnostics/{symbol}_transition_quality.csv", state_diagnostics.transition_quality)
        self.artifacts.write_frame(f"state_diagnostics/{symbol}_state_tradability.csv", state_diagnostics.state_tradability)
        self.artifacts.write_frame(f"state_diagnostics/{symbol}_sparse_states.csv", state_diagnostics.sparse_states)
        self.artifacts.write_json(
            f"markov/{symbol}_diagnostics.json",
            {
                "accuracy": analysis.accuracy,
                "state_confidence": analysis.state_confidence.to_dict(),
                "state_entropy": analysis.state_entropy.to_dict(),
            },
        )
        self.artifacts.write_json(f"state_diagnostics/{symbol}_summary.json", state_diagnostics.summary)
        for name, artifact in model_artifacts.items():
            if isinstance(artifact, pd.DataFrame):
                self.artifacts.write_frame(f"state_diagnostics/{symbol}_{name}.csv", artifact)
            else:
                self.artifacts.write_json(f"state_diagnostics/{symbol}_{name}.json", artifact)

    def _write_global_artifacts(
        self,
        opportunities: pd.DataFrame,
        backtest,
        metadata: dict[str, object],
        selection_candidates: pd.DataFrame,
    ) -> None:
        self.artifacts.write_frame("opportunities.csv", opportunities)
        self.artifacts.write_frame("backtest/equity_curve.csv", backtest.equity_curve)
        self.artifacts.write_frame("backtest/trades.csv", backtest.trades)
        self.artifacts.write_frame("backtest/orders.csv", backtest.orders)
        self.artifacts.write_frame("analytics/performance_by_state.csv", backtest.state_performance)
        self.artifacts.write_frame("analytics/performance_by_transition.csv", backtest.transition_performance)
        self.artifacts.write_frame("analytics/subperiod_performance.csv", backtest.subperiod_performance)
        self.artifacts.write_frame(
            "analytics/performance_by_volatility_regime.csv",
            performance_by_volatility_regime(
                backtest.trades,
                min_group_observations=self.config.research.diagnostics.min_group_observations,
            ),
        )
        self.artifacts.write_frame(
            "analytics/performance_by_asset_class.csv",
            performance_by_asset_class(
                backtest.trades,
                min_group_observations=self.config.research.diagnostics.min_group_observations,
            ),
        )
        self.artifacts.write_frame(
            "analytics/state_edge_summary.csv",
            state_edge_summary(
                backtest.trades,
                min_group_observations=self.config.research.diagnostics.min_group_observations,
            ),
        )
        self.artifacts.write_frame(
            "analytics/transition_edge_summary.csv",
            transition_edge_summary(
                backtest.trades,
                min_group_observations=self.config.research.diagnostics.min_group_observations,
            ),
        )
        quality_frames = quality_report_frames(
            backtest.trades,
            min_group_observations=self.config.research.diagnostics.min_group_observations,
            top_n=self.config.research.diagnostics.top_n_trades,
        )
        for name, frame in quality_frames.items():
            self.artifacts.write_frame(f"analytics/{name}.csv", frame)
        selection_frames = selection_report_frames(selection_candidates)
        for name, frame in selection_frames.items():
            self.artifacts.write_frame(f"analytics/{name}.csv", frame)
        diagnostics = trade_diagnostics(backtest.trades, top_n=self.config.research.diagnostics.top_n_trades)
        self.artifacts.write_frame("analytics/best_trades.csv", diagnostics["best_trades"])
        self.artifacts.write_frame("analytics/worst_trades.csv", diagnostics["worst_trades"])
        self.artifacts.write_frame("analytics/failure_modes.csv", diagnostics["failure_modes"])
        self.artifacts.write_json("backtest/metrics.json", backtest.metrics)
        self.artifacts.write_json("run_summary.json", {"metadata": metadata, "config": self.config.model_dump(mode="json")})
