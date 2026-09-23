from __future__ import annotations

import numpy as np
import pandas as pd

from trader_engine.core.config import FeatureConfig, StateConfig
from trader_engine.states.base import BaseStateModel
from trader_engine.states.utils import SparseStateReducer, infer_state_family


class CompositeRuleStateModel(BaseStateModel):
    def __init__(self, state_config: StateConfig, feature_config: FeatureConfig) -> None:
        self.state_config = state_config
        self.feature_config = feature_config

    def classify(self, frame: pd.DataFrame) -> pd.DataFrame:
        classified = frame.copy()
        ma_windows = sorted(set(self.feature_config.moving_average_windows))
        fast = ma_windows[0]
        slow = ma_windows[-1]
        momentum_horizon = (
            self.state_config.short_horizon
            if f"return_{self.state_config.short_horizon}" in classified.columns
            else (5 if 5 in self.feature_config.return_horizons else self.feature_config.return_horizons[0])
        )
        vol_window = self.feature_config.volatility_window
        z_window = self.feature_config.zscore_window
        rsi_window = self.feature_config.rsi_window

        trend_metric = classified[f"ma_{fast}"] / classified[f"ma_{slow}"] - 1.0
        classified["state_trend"] = np.select(
            [
                trend_metric > self.state_config.trend_threshold,
                trend_metric < -self.state_config.trend_threshold,
            ],
            ["up", "down"],
            default="neutral",
        )

        volatility_metric = classified[f"vol_zscore_{vol_window}"]
        classified["state_volatility"] = np.select(
            [
                volatility_metric > self.state_config.volatility_zscore_threshold,
                volatility_metric < -self.state_config.volatility_zscore_threshold,
            ],
            ["high", "low"],
            default="normal",
        )

        momentum_metric = classified[f"return_{momentum_horizon}"]
        classified["state_momentum"] = np.select(
            [
                momentum_metric > self.state_config.momentum_threshold,
                momentum_metric < -self.state_config.momentum_threshold,
            ],
            ["strong", "weak"],
            default="neutral",
        )

        extension_metric = classified[f"zscore_close_{z_window}"]
        rsi = classified[f"rsi_{rsi_window}"]
        classified["state_extension"] = np.select(
            [
                (extension_metric > self.state_config.extension_threshold)
                | (rsi > self.state_config.rsi_overbought),
                (extension_metric < -self.state_config.extension_threshold)
                | (rsi < self.state_config.rsi_oversold),
            ],
            ["overbought", "oversold"],
            default="normal",
        )

        classified["state"] = (
            classified["state_trend"]
            + "|"
            + classified["state_volatility"]
            + "|"
            + classified["state_momentum"]
            + "|"
            + classified["state_extension"]
        )
        classified["state_family"] = infer_state_family(classified)
        return classified

    def model_artifacts(self) -> dict[str, pd.DataFrame | dict]:
        return {
            "model_settings": pd.DataFrame(
                [
                    {"parameter": "model_name", "value": self.state_config.model_name},
                    {"parameter": "trend_threshold", "value": self.state_config.trend_threshold},
                    {"parameter": "volatility_zscore_threshold", "value": self.state_config.volatility_zscore_threshold},
                    {"parameter": "momentum_threshold", "value": self.state_config.momentum_threshold},
                    {"parameter": "extension_threshold", "value": self.state_config.extension_threshold},
                ]
            )
        }


class RichCompositeRuleStateModel(BaseStateModel):
    def __init__(self, state_config: StateConfig, feature_config: FeatureConfig) -> None:
        self.state_config = state_config
        self.feature_config = feature_config
        self.sparse_reducer: SparseStateReducer | None = None

    def fit(self, frame: pd.DataFrame) -> "RichCompositeRuleStateModel":
        component_frame = self._component_frame(frame)
        if self.state_config.rich_merge_sparse_states:
            threshold = self.state_config.rich_min_state_observations or self.state_config.sparse_state_warning_threshold
            merge_priority = self._merge_priority()
            self.sparse_reducer = SparseStateReducer(
                component_names=component_frame.columns.tolist(),
                merge_priority=[column for column in merge_priority if column in component_frame.columns],
                min_observations=threshold,
                merge_label=self.state_config.rich_merge_label,
            ).fit(component_frame)
        else:
            self.sparse_reducer = None
        return self

    def classify(self, frame: pd.DataFrame) -> pd.DataFrame:
        classified = frame.copy()
        component_frame = self._component_frame(frame)
        for column in component_frame.columns:
            classified[column] = component_frame[column]
        if self.sparse_reducer is not None:
            classified["state"] = self.sparse_reducer.transform(component_frame)
        else:
            classified["state"] = component_frame.astype(str).agg("|".join, axis=1)
        classified["state_family"] = infer_state_family(classified)
        return classified

    def model_artifacts(self) -> dict[str, pd.DataFrame | dict]:
        artifacts: dict[str, pd.DataFrame | dict] = {
            "model_settings": pd.DataFrame(
                [
                    {"parameter": "model_name", "value": self.state_config.model_name},
                    {"parameter": "short_horizon", "value": self.state_config.short_horizon},
                    {"parameter": "medium_horizon", "value": self.state_config.medium_horizon},
                    {"parameter": "long_horizon", "value": self.state_config.long_horizon},
                    {"parameter": "rich_components", "value": ",".join(self.state_config.rich_components)},
                    {"parameter": "acceleration_threshold", "value": self.state_config.acceleration_threshold},
                    {"parameter": "vol_regime_threshold", "value": self.state_config.vol_regime_threshold},
                    {"parameter": "atr_move_threshold", "value": self.state_config.atr_move_threshold},
                    {"parameter": "volume_expansion_threshold", "value": self.state_config.volume_expansion_threshold},
                    {"parameter": "volume_contraction_threshold", "value": self.state_config.volume_contraction_threshold},
                    {"parameter": "persistence_thresholds", "value": ",".join(map(str, self.state_config.persistence_thresholds))},
                    {"parameter": "rich_collapse_rsi_zones", "value": self.state_config.rich_collapse_rsi_zones},
                    {"parameter": "rich_collapse_volatility_regimes", "value": self.state_config.rich_collapse_volatility_regimes},
                    {"parameter": "rich_collapse_momentum_regimes", "value": self.state_config.rich_collapse_momentum_regimes},
                    {"parameter": "rich_merge_sparse_states", "value": self.state_config.rich_merge_sparse_states},
                ]
            )
        }
        if self.sparse_reducer is not None:
            artifacts.update(self.sparse_reducer.artifacts())
        return artifacts

    def _component_frame(self, frame: pd.DataFrame) -> pd.DataFrame:
        classified = frame.copy()
        short = self.state_config.short_horizon
        medium = self.state_config.medium_horizon
        long = self.state_config.long_horizon
        vol_window = self.feature_config.volatility_window
        rsi_window = self.feature_config.rsi_window

        short_return = self._series(classified, f"return_{short}")
        medium_return = self._series(classified, f"return_{medium}")
        long_return = self._series(classified, f"return_{long}")
        ma20 = self._series(classified, "ma_gap_20")
        ma50 = self._series(classified, "ma_gap_50")
        ma200 = self._series(classified, "ma_gap_200")

        trend_score = (
            (short_return > 0).astype(int)
            + (medium_return > 0).astype(int)
            + (long_return > 0).astype(int)
            + (ma20 > 0).astype(int)
            + (ma50 > 0).astype(int)
            + (ma200 > 0).astype(int)
            - (short_return < 0).astype(int)
            - (medium_return < 0).astype(int)
            - (long_return < 0).astype(int)
            - (ma20 < 0).astype(int)
            - (ma50 < 0).astype(int)
            - (ma200 < 0).astype(int)
        )
        state_trend = np.select(
            [
                trend_score >= 4,
                trend_score <= -4,
            ],
            ["up", "down"],
            default="mixed",
        )

        vol_expansion = self._series(classified, f"vol_expansion_{vol_window}")
        vol_zscore = self._series(classified, f"vol_zscore_{vol_window}")
        state_volatility = np.select(
            [
                (vol_expansion > self.state_config.vol_regime_threshold)
                | (vol_zscore > self.state_config.volatility_zscore_threshold),
                (vol_expansion < -self.state_config.vol_regime_threshold)
                | (vol_zscore < -self.state_config.volatility_zscore_threshold),
            ],
            ["expanding", "compressing"],
            default="stable",
        )
        if self.state_config.rich_collapse_volatility_regimes:
            state_volatility = np.where(state_volatility == "stable", "stable", "unstable")

        accel_columns = [
            column
            for column in [
                f"return_accel_{short}_{medium}",
                f"return_accel_{medium}_{long}",
            ]
            if column in classified.columns
        ]
        accel_signal = classified[accel_columns].mean(axis=1) if accel_columns else pd.Series(0.0, index=classified.index)
        state_acceleration = np.select(
            [
                accel_signal > self.state_config.acceleration_threshold,
                accel_signal < -self.state_config.acceleration_threshold,
            ],
            ["accelerating", "decelerating"],
            default="steady",
        )
        if self.state_config.rich_collapse_momentum_regimes:
            state_acceleration = np.where(state_acceleration == "steady", "steady", "active")

        rsi = self._series(classified, f"rsi_{rsi_window}")
        stretched_up = (rsi >= self.state_config.rsi_overbought) | (
            self._series(classified, "atr_normalized_close_to_sma20") >= self.state_config.atr_move_threshold
        )
        stretched_down = (rsi <= self.state_config.rsi_oversold) | (
            self._series(classified, "atr_normalized_close_to_sma20") <= -self.state_config.atr_move_threshold
        )
        state_extension = np.select(
            [stretched_up, stretched_down],
            ["stretched_up", "stretched_down"],
            default="normal",
        )
        if self.state_config.rich_collapse_rsi_zones:
            state_extension = np.where(state_extension == "normal", "normal", "stretched")

        volume_ratio = self._series(classified, f"volume_ratio_{self.feature_config.volume_window}")
        state_volume = np.select(
            [
                volume_ratio >= self.state_config.volume_expansion_threshold,
                volume_ratio <= self.state_config.volume_contraction_threshold,
            ],
            ["expanding", "contracting"],
            default="normal",
        )

        trend_persistence = self._series(classified, "trend_persistence")
        thresholds = sorted(self.state_config.persistence_thresholds)
        state_persistence = np.select(
            [
                trend_persistence <= thresholds[0],
                trend_persistence <= thresholds[-1],
            ],
            ["fresh", "established"],
            default="extended",
        )

        component_frame = pd.DataFrame(
            {
                "state_trend": state_trend,
                "state_volatility": state_volatility,
                "state_acceleration": state_acceleration,
                "state_extension": state_extension,
                "state_volume": state_volume,
                "state_persistence": state_persistence,
            },
            index=classified.index,
        )

        components = {
            "trend": "state_trend",
            "volatility": "state_volatility",
            "acceleration": "state_acceleration",
            "extension": "state_extension",
            "volume": "state_volume",
            "persistence": "state_persistence",
        }
        columns = [components[name] for name in self.state_config.rich_components if name in components]
        if not columns:
            columns = ["state_trend", "state_volatility", "state_extension"]
        return component_frame[columns]

    def _merge_priority(self) -> list[str]:
        if self.state_config.rich_merge_order:
            return [f"state_{name}" for name in self.state_config.rich_merge_order]
        return [f"state_{name}" for name in reversed(self.state_config.rich_components)]

    @staticmethod
    def _series(frame: pd.DataFrame, column: str) -> pd.Series:
        if column in frame.columns:
            return frame[column]
        return pd.Series(0.0, index=frame.index)
