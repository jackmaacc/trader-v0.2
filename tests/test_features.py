from __future__ import annotations

import numpy as np
import pandas as pd

from trader_engine.core.config import FeatureConfig
from trader_engine.core.models import AssetClass
from trader_engine.features.engine import FeatureEngineer


def test_feature_engine_generates_expected_columns() -> None:
    index = pd.date_range("2022-01-01", periods=120, freq="D")
    base = np.linspace(100.0, 140.0, len(index))
    oscillation = np.sin(np.linspace(0.0, 10.0, len(index)))
    close = base + oscillation
    frame = pd.DataFrame(
        {
            "open": close - 0.5,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.linspace(1_000_000, 2_000_000, len(index)),
        },
        index=index,
    )

    engineer = FeatureEngineer(FeatureConfig())
    features = engineer.transform(frame, AssetClass.EQUITY)

    expected_columns = {
        "return_1",
        "return_3",
        "return_5",
        "return_rate_20",
        "atr_14",
        "realized_vol_20",
        "vol_expansion_20",
        "rsi_14",
        "rsi_zone",
        "ma_10",
        "ma_50",
        "sma_distance_50",
        "zscore_close_20",
        "return_zscore_5_20",
        "return_accel_3_10",
        "trend_persistence",
        "volume_regime_20",
        "dollar_volume_20",
        "range_efficiency_20",
    }
    assert expected_columns.issubset(features.columns)
    assert features["atr_14"].dropna().iloc[-1] > 0
    assert features["realized_vol_20"].dropna().iloc[-1] > 0
