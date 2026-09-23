from __future__ import annotations

from trader_engine.core.config import AppConfig
from trader_engine.core.models import AssetClass
from trader_engine.research.parameters import apply_parameter_overrides, generate_parameter_grid


def test_asset_class_override_resolution() -> None:
    config = AppConfig.model_validate(
        {
            "signals": {"min_expected_value": 0.001},
            "asset_overrides": {
                "crypto": {
                    "signals": {"min_expected_value": 0.003},
                    "markov": {"transition_lookback_bars": 90},
                }
            },
        }
    )

    equity = config.resolved_for_asset_class(AssetClass.EQUITY)
    crypto = config.resolved_for_asset_class(AssetClass.CRYPTO)

    assert equity.signals.min_expected_value == 0.001
    assert crypto.signals.min_expected_value == 0.003
    assert crypto.markov.transition_lookback_bars == 90


def test_parameter_override_application_and_grid_generation() -> None:
    grid = generate_parameter_grid(
        {
            "states.trend_threshold": [0.01, 0.02],
            "asset_overrides.crypto.signals.min_expected_value": [0.001, 0.002],
        },
        max_combinations=4,
    )
    assert len(grid) == 4

    config = AppConfig()
    updated = apply_parameter_overrides(
        config,
        {
            "states.trend_threshold": 0.02,
            "asset_overrides.crypto.signals.min_expected_value": 0.002,
        },
    )

    assert updated.states.trend_threshold == 0.02
    assert updated.asset_overrides["crypto"].signals.min_expected_value == 0.002
