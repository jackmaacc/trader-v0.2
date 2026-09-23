from __future__ import annotations

import pandas as pd

from trader_engine.analytics.quality import edge_quality
from trader_engine.core.config import AppConfig, FeatureConfig, MarkovConfig, SignalConfig
from trader_engine.core.models import AssetClass, UniverseMember
from trader_engine.markov.engine import MarkovAnalyzer
from trader_engine.research.quality import build_quality_gate_tables
from trader_engine.research.selection import apply_opportunity_caps
from trader_engine.signals.engine import QualityGateTables, SignalEngine


def test_build_quality_gate_tables_are_asset_class_aware() -> None:
    trades = pd.DataFrame(
        {
            "asset_class": ["equity", "equity", "crypto", "crypto"],
            "entry_state": ["up", "up", "up", "up"],
            "entry_transition_setup": ["up -> up"] * 4,
            "entry_state_family": ["up|stable", "up|stable", "up", "up"],
            "return_pct": [0.02, 0.01, -0.03, -0.01],
            "fold_id": [1, 2, 1, 2],
            "symbol": ["EQ1", "EQ2", "CR1", "CR2"],
        }
    )

    tables, results = build_quality_gate_tables(trades, min_group_observations=1)

    assert not tables.state_quality.empty
    assert {"asset_class", "state", "quality_score", "active_fold_count", "positive_fold_fraction"}.issubset(
        tables.state_quality.columns
    )
    assert len(tables.state_quality.loc[tables.state_quality["state"] == "up"]) == 2
    assert not results["state"].fold_consistency.empty


def test_signal_engine_quality_gate_blocks_missing_asset_class_record() -> None:
    signal_engine = SignalEngine(
        SignalConfig(
            min_expected_value=0.0,
            min_confidence=0.0,
            quality_gate_mode="both",
            min_state_quality_score=0.5,
            min_transition_quality_score=0.5,
            min_state_trade_count=1,
            min_transition_trade_count=1,
        ),
        MarkovConfig(
            transition_horizon_bars=1,
            forward_return_horizon_bars=1,
            min_state_observations=1,
            laplace_smoothing=1.0,
        ),
        FeatureConfig(),
    )
    analyzer = MarkovAnalyzer(
        MarkovConfig(
            transition_horizon_bars=1,
            forward_return_horizon_bars=1,
            min_state_observations=1,
            laplace_smoothing=1.0,
        )
    )
    analysis = analyzer.analyze(
        pd.DataFrame(
            {
                "state": ["up", "up", "up", "up"],
                "close": [100.0, 101.0, 102.0, 103.0],
            }
        )
    )
    row = pd.Series(
        {
            "state": "up",
            "state_family": "up|stable",
            "realized_vol_20": 0.05,
            "dollar_volume_20": 2_000_000.0,
        }
    )
    state_quality = pd.DataFrame(
        [
            {
                "asset_class": "equity",
                "state": "up",
                "sample_size": 4,
                "quality_score": 0.8,
                "active_fold_count": 2,
                "positive_fold_fraction": 1.0,
                "expectancy_variance": 0.0,
                "sharpe_variance": 0.0,
                "consistency_score": 0.9,
            }
        ]
    )
    transition_quality = pd.DataFrame(
        [
            {
                "asset_class": "equity",
                "transition_setup": "up -> up",
                "sample_size": 4,
                "quality_score": 0.8,
                "active_fold_count": 2,
                "positive_fold_fraction": 1.0,
                "expectancy_variance": 0.0,
                "sharpe_variance": 0.0,
                "consistency_score": 0.9,
            }
        ]
    )

    gated_snapshot = signal_engine.evaluate_snapshot(
        member=UniverseMember(symbol="EQ", asset_class=AssetClass.EQUITY),
        row=row,
        analysis=analysis,
        quality_tables=QualityGateTables(state_quality=state_quality, transition_quality=transition_quality),
    )
    blocked_snapshot = signal_engine.evaluate_snapshot(
        member=UniverseMember(symbol="BTC", asset_class=AssetClass.CRYPTO),
        row=row,
        analysis=analysis,
        quality_tables=QualityGateTables(state_quality=state_quality, transition_quality=transition_quality),
    )

    assert gated_snapshot.gate_passed is True
    assert gated_snapshot.direction.value == "long"
    assert blocked_snapshot.gate_passed is False
    assert blocked_snapshot.direction.value == "flat"


def test_edge_quality_reports_fold_consistency_columns() -> None:
    trades = pd.DataFrame(
        {
            "entry_state": ["a", "a", "a", "b"],
            "return_pct": [0.02, -0.01, 0.03, -0.02],
            "fold_id": [1, 2, 3, 1],
            "symbol": ["X", "X", "X", "Y"],
        }
    )

    result = edge_quality(trades, "entry_state", min_group_observations=1)

    assert {"active_fold_count", "positive_fold_fraction", "expectancy_variance", "sharpe_variance"}.issubset(
        result.summary.columns
    )


def test_signal_engine_does_not_gate_validation_when_quality_tables_absent() -> None:
    signal_engine = SignalEngine(
        SignalConfig(
            min_expected_value=0.0,
            min_confidence=0.0,
            selection_mode="hybrid",
            quality_gate_components=["state", "transition", "consistency"],
            min_state_quality_score=0.9,
            min_transition_quality_score=0.9,
            min_fold_consistency_score=0.9,
        ),
        MarkovConfig(
            transition_horizon_bars=1,
            forward_return_horizon_bars=1,
            min_state_observations=1,
            laplace_smoothing=1.0,
        ),
        FeatureConfig(),
    )
    analyzer = MarkovAnalyzer(
        MarkovConfig(
            transition_horizon_bars=1,
            forward_return_horizon_bars=1,
            min_state_observations=1,
            laplace_smoothing=1.0,
        )
    )
    analysis = analyzer.analyze(pd.DataFrame({"state": ["up", "up", "up", "up"], "close": [100.0, 101.0, 102.0, 103.0]}))
    row = pd.Series(
        {
            "state": "up",
            "state_family": "up|stable",
            "realized_vol_20": 0.05,
            "dollar_volume_20": 2_000_000.0,
        }
    )

    snapshot = signal_engine.evaluate_snapshot(
        member=UniverseMember(symbol="EQ", asset_class=AssetClass.EQUITY),
        row=row,
        analysis=analysis,
        quality_tables=QualityGateTables(),
    )

    assert snapshot.direction.value == "long"
    assert snapshot.gate_passed is True


def test_apply_opportunity_caps_limits_candidates_per_timestamp() -> None:
    index = pd.date_range("2024-01-01", periods=1, freq="D")
    frames = {
        "A": pd.DataFrame(
            {
                "signal": ["long"],
                "candidate_signal": ["long"],
                "selection_passed": [True],
                "signal_score": [0.9],
                "asset_class": ["equity"],
                "selection_cap_overall_limit": [1],
                "selection_cap_asset_class_limit": [1],
                "selection_cap_model_limit": [1],
            },
            index=index,
        ),
        "B": pd.DataFrame(
            {
                "signal": ["long"],
                "candidate_signal": ["long"],
                "selection_passed": [True],
                "signal_score": [0.4],
                "asset_class": ["equity"],
                "selection_cap_overall_limit": [1],
                "selection_cap_asset_class_limit": [1],
                "selection_cap_model_limit": [1],
            },
            index=index,
        ),
    }

    result = apply_opportunity_caps(frames)

    assert result.frames_by_symbol["A"].iloc[0]["signal"] == "long"
    assert result.frames_by_symbol["B"].iloc[0]["signal"] == "flat"
    assert int(result.candidates["final_selection_passed"].sum()) == 1


def test_selection_policy_resolution_is_asset_class_aware() -> None:
    config = AppConfig.model_validate(
        {
            "signals": {
                "selection_policy_name": "policy_a",
                "selection_mode": "hard_gate_only",
            },
            "research": {
                "selection_policies": {
                    "policy_a": {
                        "signals": {
                            "selection_mode": "soft_scoring_only",
                            "hard_filter_components": ["sample_size"],
                            "min_sample_size_score": 0.25,
                        },
                        "asset_signals": {
                            "crypto": {
                                "min_sample_size_score": 0.40,
                            }
                        },
                    }
                }
            },
        }
    )

    equity = config.resolved_for_asset_class("equity").signals
    crypto = config.resolved_for_asset_class("crypto").signals

    assert equity.selection_mode == "soft_scoring_only"
    assert equity.hard_filter_components == ["sample_size"]
    assert equity.min_sample_size_score == 0.25
    assert crypto.min_sample_size_score == 0.40
