from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trader_engine.core.config import AppConfig, FeatureConfig, StateConfig
from trader_engine.core.models import AssetClass, UniverseMember
from trader_engine.features.engine import FeatureEngineer
from trader_engine.research.context import ResearchContext
from trader_engine.research.exit_comparison import ExitComparisonRunner
from trader_engine.research.model_comparison import StateModelComparisonRunner
from trader_engine.states import build_state_model


def _synthetic_raw_frame(periods: int = 320) -> pd.DataFrame:
    index = pd.date_range("2022-01-01", periods=periods, freq="D")
    trend = np.linspace(100.0, 150.0, periods)
    cycle = np.sin(np.linspace(0.0, 24.0, periods)) * 4.0
    shock = np.where((np.arange(periods) % 45) < 6, -2.5, 0.0)
    close = trend + cycle + shock
    return pd.DataFrame(
        {
            "open": close - 0.4,
            "high": close + 1.2,
            "low": close - 1.2,
            "close": close,
            "volume": np.linspace(900_000, 2_200_000, periods),
        },
        index=index,
    )


def test_contextual_rich_state_model_emits_state_metadata() -> None:
    raw = _synthetic_raw_frame()
    feature_config = FeatureConfig()
    features = FeatureEngineer(feature_config).transform(raw, AssetClass.EQUITY)
    state_config = StateConfig(
        model_name="rich_composite_rule",
        contextualize_states=True,
        append_previous_state=True,
        append_state_age_bucket=True,
        append_entry_bias=True,
    )

    model = build_state_model(state_config, feature_config)
    classified = model.fit_transform(features)

    assert {"state", "base_state", "state_age", "state_age_bucket", "entry_bias"}.issubset(classified.columns)
    assert classified["state"].dropna().nunique() > 1
    assert classified["state_age"].dropna().iloc[-1] >= 1


def test_feature_bin_state_model_merges_sparse_states() -> None:
    raw = _synthetic_raw_frame()
    feature_config = FeatureConfig()
    features = FeatureEngineer(feature_config).transform(raw, AssetClass.EQUITY)
    state_config = StateConfig(
        model_name="feature_bins",
        bin_features=["return_5", "return_20", "realized_vol_20", "rsi_14", "volume_ratio_20"],
        bin_count=4,
        bin_merge_sparse_states=True,
        bin_min_state_observations=40,
        bin_merge_order=["volume_ratio_20", "rsi_14", "realized_vol_20"],
    )

    model = build_state_model(state_config, feature_config)
    classified = model.fit_transform(features)
    artifacts = model.model_artifacts()

    assert "state" in classified.columns
    assert classified["state"].dropna().str.contains("any|rare_state", regex=True).any()
    assert "sparse_merge_levels" in artifacts
    assert not artifacts["sparse_merge_levels"].empty


def test_kmeans_state_model_emits_cluster_artifacts() -> None:
    pytest.importorskip("sklearn")
    raw = _synthetic_raw_frame()
    feature_config = FeatureConfig()
    features = FeatureEngineer(feature_config).transform(raw, AssetClass.EQUITY)
    state_config = StateConfig(
        model_name="kmeans",
        cluster_count=4,
        cluster_features=[
            "return_5",
            "return_20",
            "return_60",
            "realized_vol_20",
            "ma_gap_20",
            "ma_gap_50",
            "rsi_14",
            "trend_persistence",
        ],
    )

    model = build_state_model(state_config, feature_config)
    classified = model.fit_transform(features)
    artifacts = model.model_artifacts()

    assert {"state", "state_cluster"}.issubset(classified.columns)
    assert classified["state"].dropna().str.startswith("cluster_").any()
    assert "cluster_centers" in artifacts
    assert not artifacts["cluster_centers"].empty


def test_hybrid_regime_kmeans_emits_regime_and_cluster_artifacts() -> None:
    pytest.importorskip("sklearn")
    raw = _synthetic_raw_frame()
    feature_config = FeatureConfig()
    features = FeatureEngineer(feature_config).transform(raw, AssetClass.EQUITY)
    state_config = StateConfig(
        model_name="hybrid_regime_kmeans",
        hybrid_anchor="trend",
        hybrid_cluster_count=3,
        hybrid_min_regime_samples=35,
        cluster_min_size=12,
        cluster_features=[
            "return_5",
            "return_20",
            "realized_vol_20",
            "ma_gap_20",
            "rsi_14",
            "trend_persistence",
        ],
    )

    model = build_state_model(state_config, feature_config)
    classified = model.fit_transform(features)
    artifacts = model.model_artifacts()

    assert {"state", "state_anchor_regime", "state_cluster"}.issubset(classified.columns)
    assert classified["state_anchor_regime"].dropna().nunique() >= 2
    assert "hybrid_regime_summary" in artifacts
    assert not artifacts["hybrid_regime_summary"].empty


def test_state_model_comparison_runner_emits_summary_and_model_artifacts() -> None:
    config = AppConfig.model_validate(
        {
            "data": {"min_history_bars": 80},
            "signals": {
                "min_training_bars": 60,
                "min_expected_value": 0.0,
                "min_confidence": 0.0,
                "allow_short_equities": False,
                "allow_short_crypto": False,
            },
            "risk": {
                "initial_capital": 10_000.0,
                "max_position_pct": 0.25,
                "max_gross_exposure": 1.0,
                "max_concurrent_positions": 2,
                "stop_atr_multiple": 3.0,
                "max_daily_loss_pct": 0.5,
            },
            "backtest": {
                "hold_bars": 5,
                "commission_bps": 0.0,
                "slippage_bps_equity": 0.0,
                "slippage_bps_crypto": 0.0,
            },
            "research": {
                "walk_forward": {
                    "train_bars": 140,
                    "validation_bars": 40,
                    "test_bars": 40,
                    "step_bars": 40,
                    "minimum_symbol_train_bars": 90,
                },
                "diagnostics": {"top_n_trades": 5, "min_group_observations": 1},
                "state_model_comparison": {
                    "enabled": True,
                    "models": [
                        {
                            "name": "legacy_rule",
                            "overrides": {"states.model_name": "composite_rule"},
                        },
                        {
                            "name": "feature_bins",
                            "overrides": {
                                "states.model_name": "feature_bins",
                                "states.bin_merge_sparse_states": True,
                                "states.bin_min_state_observations": 30,
                            },
                        },
                    ],
                },
            },
        }
    )

    context = ResearchContext(config)
    members = {"TEST": UniverseMember(symbol="TEST", asset_class=AssetClass.EQUITY)}
    raw = {"TEST": _synthetic_raw_frame()}

    runner = StateModelComparisonRunner(context)
    result = runner.run(members_by_symbol=members, raw_by_symbol=raw)

    assert not result.summary.empty
    assert {
        "model_name",
        "mean_test_sharpe",
        "state_coverage",
        "transition_stability",
        "effective_state_coverage",
        "share_states_above_threshold",
        "trade_generating_state_percentage",
        "tradability_score",
    }.issubset(result.summary.columns)
    assert not result.fold_metrics.empty
    assert "model_name" in result.fold_metrics.columns
    assert "feature_bins" in result.model_outputs
    assert "bin_edges" in result.model_outputs["feature_bins"]
    assert not result.model_outputs["feature_bins"]["bin_edges"].empty
    assert "state_tradability" in result.model_outputs["feature_bins"]


def test_exit_comparison_runner_emits_summary_and_combo_outputs() -> None:
    config = AppConfig.model_validate(
        {
            "data": {"min_history_bars": 80},
            "signals": {
                "min_training_bars": 60,
                "min_expected_value": 0.0,
                "min_confidence": 0.0,
                "allow_short_equities": False,
                "allow_short_crypto": False,
                "quality_gate_mode": "state",
                "min_state_quality_score": 0.0,
                "min_state_trade_count": 1,
                "min_state_active_folds": 1,
            },
            "risk": {
                "initial_capital": 10_000.0,
                "max_position_pct": 0.25,
                "max_gross_exposure": 1.0,
                "max_concurrent_positions": 2,
                "stop_atr_multiple": 3.0,
                "max_daily_loss_pct": 0.5,
            },
            "backtest": {
                "hold_bars": 5,
                "commission_bps": 0.0,
                "slippage_bps_equity": 0.0,
                "slippage_bps_crypto": 0.0,
            },
            "research": {
                "walk_forward": {
                    "train_bars": 140,
                    "validation_bars": 40,
                    "test_bars": 40,
                    "step_bars": 40,
                    "minimum_symbol_train_bars": 90,
                },
                "diagnostics": {"top_n_trades": 5, "min_group_observations": 1},
                "state_model_comparison": {
                    "enabled": True,
                    "models": [
                        {
                            "name": "feature_bins_dense",
                            "overrides": {
                                "states.model_name": "feature_bins",
                                "states.bin_merge_sparse_states": True,
                                "states.bin_min_state_observations": 30,
                            },
                        }
                    ],
                },
                "exit_comparison": {
                    "enabled": True,
                    "model_names": ["feature_bins_dense"],
                    "profiles": [
                        {
                            "name": "fixed_hold",
                            "overrides": {
                                "backtest.hold_bars": 5,
                                "backtest.exit_on_state_change": False,
                                "backtest.use_atr_stop": False,
                            },
                        },
                        {
                            "name": "state_change_exit",
                            "overrides": {
                                "backtest.hold_bars": 20,
                                "backtest.exit_on_state_change": True,
                                "backtest.use_atr_stop": False,
                            },
                        },
                    ],
                },
            },
        }
    )

    context = ResearchContext(config)
    members = {"TEST": UniverseMember(symbol="TEST", asset_class=AssetClass.EQUITY)}
    raw = {"TEST": _synthetic_raw_frame()}

    runner = ExitComparisonRunner(context)
    result = runner.run(members_by_symbol=members, raw_by_symbol=raw)

    assert not result.summary.empty
    assert {"combo_name", "model_name", "exit_profile", "mean_test_sharpe", "exit_score"}.issubset(
        result.summary.columns
    )
    assert not result.fold_metrics.empty
    combo_name = result.summary.iloc[0]["combo_name"]
    assert combo_name in result.combo_outputs
    assert "aggregate_metrics" in result.combo_outputs[combo_name]
