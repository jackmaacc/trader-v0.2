from __future__ import annotations

import numpy as np
import pandas as pd

from trader_engine.core.config import AppConfig
from trader_engine.core.models import AssetClass, UniverseMember
from trader_engine.research.context import ResearchContext
from trader_engine.research.gate_ablation import GateAblationRunner
from trader_engine.research.policy_comparison import SelectionPolicyComparisonRunner
from trader_engine.research.sweeps import ParameterSweepRunner
from trader_engine.research.walk_forward import WalkForwardRunner


def _synthetic_raw_frame(periods: int = 220) -> pd.DataFrame:
    index = pd.date_range("2022-01-01", periods=periods, freq="D")
    base = np.linspace(100.0, 140.0, periods)
    oscillation = np.sin(np.linspace(0.0, 16.0, periods)) * 3.0
    close = base + oscillation
    return pd.DataFrame(
        {
            "open": close - 0.5,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.linspace(1_000_000, 1_500_000, periods),
        },
        index=index,
    )


def _test_config() -> AppConfig:
    return AppConfig.model_validate(
        {
            "data": {"min_history_bars": 50},
            "signals": {
                "min_training_bars": 40,
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
                    "train_bars": 90,
                    "validation_bars": 30,
                    "test_bars": 30,
                    "step_bars": 30,
                    "minimum_symbol_train_bars": 60,
                },
                "parameter_sweep": {
                    "enabled": True,
                    "search_space": {"states.trend_threshold": [0.008, 0.012]},
                    "max_combinations": 2,
                    "rank_metric": "mean_test_sharpe",
                },
                "state_model_comparison": {
                    "models": [
                        {
                            "name": "baseline",
                            "selection_policy": "policy_a",
                            "overrides": {},
                        }
                    ]
                },
                "selection_policies": {
                    "policy_a": {
                        "signals": {
                            "selection_mode": "soft_scoring_only",
                            "hard_filter_components": ["sample_size"],
                            "min_sample_size_score": 0.2,
                        }
                    }
                },
                "selection_policy_comparison": {
                    "enabled": True,
                    "experiments": [
                        {
                            "name": "baseline_policy",
                            "model_name": "baseline",
                            "selection_policy": "policy_a",
                            "category": "policy",
                        }
                    ],
                },
                "gate_ablation": {
                    "enabled": True,
                    "model_names": ["baseline"],
                    "selection_mode": "hybrid",
                },
                "diagnostics": {"top_n_trades": 5, "min_group_observations": 1},
            },
        }
    )


def test_walk_forward_runner_produces_fold_metrics() -> None:
    config = _test_config()
    context = ResearchContext(config)
    dataset = context.prepare_dataset(
        members_by_symbol={"TEST": UniverseMember(symbol="TEST", asset_class=AssetClass.EQUITY)},
        raw_by_symbol={"TEST": _synthetic_raw_frame()},
    )

    runner = WalkForwardRunner(context)
    result = runner.run(dataset)

    assert not result.folds.empty
    assert not result.fold_metrics.empty
    assert not result.trades.empty
    assert {"split", "total_return", "sharpe", "trade_count", "expectancy"}.issubset(result.fold_metrics.columns)
    assert {
        "entry_state_quality_score",
        "entry_transition_quality_score",
        "entry_fold_consistency_score",
        "entry_state_family",
    }.issubset(result.trades.columns)
    assert {
        "test_state_quality_summary",
        "test_state_fold_consistency",
        "test_state_family_performance",
    }.issubset(result.quality_frames.keys())
    assert {
        "test_exit_reason_performance",
        "test_state_model_performance",
        "test_holding_period_performance",
    }.issubset(result.attribution_frames.keys())
    assert {
        "test_payoff_asymmetry_summary",
        "test_adverse_excursion_distribution",
        "test_uncaptured_favorable_distribution",
    }.issubset(result.diagnostic_frames.keys())


def test_parameter_sweep_runner_emits_parameter_columns() -> None:
    config = _test_config()
    context = ResearchContext(config)
    members = {"TEST": UniverseMember(symbol="TEST", asset_class=AssetClass.EQUITY)}
    raw = {"TEST": _synthetic_raw_frame()}

    runner = ParameterSweepRunner(context)
    result = runner.run(members_by_symbol=members, raw_by_symbol=raw)

    assert len(result.parameter_definitions) == 2
    assert not result.summary.empty
    assert "param_states__trend_threshold" in result.summary.columns


def test_gate_ablation_runner_emits_summary() -> None:
    config = _test_config()
    context = ResearchContext(config)
    members = {"TEST": UniverseMember(symbol="TEST", asset_class=AssetClass.EQUITY)}
    raw = {"TEST": _synthetic_raw_frame()}

    runner = GateAblationRunner(context)
    result = runner.run(members_by_symbol=members, raw_by_symbol=raw)

    assert not result.summary.empty
    assert {"combo_name", "model_name", "ablation_name", "ablation_score"}.issubset(result.summary.columns)
    assert not result.fold_metrics.empty
    assert "baseline__all_gates" in result.combo_outputs


def test_selection_policy_comparison_runner_emits_summary() -> None:
    config = _test_config()
    context = ResearchContext(config)
    members = {"TEST": UniverseMember(symbol="TEST", asset_class=AssetClass.EQUITY)}
    raw = {"TEST": _synthetic_raw_frame()}

    runner = SelectionPolicyComparisonRunner(context)
    result = runner.run(members_by_symbol=members, raw_by_symbol=raw)

    assert not result.summary.empty
    assert {
        "experiment_name",
        "model_name",
        "selection_policy",
        "selection_mode",
        "policy_score",
    }.issubset(result.summary.columns)
    assert not result.fold_metrics.empty
    assert "baseline_policy" in result.experiment_outputs
