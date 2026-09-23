from __future__ import annotations

import numpy as np
import pandas as pd

from trader_engine.core.config import FeatureConfig
from trader_engine.core.models import AssetClass


class FeatureEngineer:
    def __init__(self, config: FeatureConfig) -> None:
        self.config = config

    def transform(self, frame: pd.DataFrame, asset_class: AssetClass) -> pd.DataFrame:
        features = frame.copy().sort_index()
        close = features["close"]
        high = features["high"]
        low = features["low"]
        volume = features["volume"]

        log_returns = np.log(close).diff()
        features["log_return_1"] = log_returns

        for horizon in self.config.return_horizons:
            features[f"return_{horizon}"] = close.pct_change(horizon)
            features[f"return_rate_{horizon}"] = features[f"return_{horizon}"] / max(horizon, 1)

        true_range = pd.concat(
            [
                high - low,
                (high - close.shift(1)).abs(),
                (low - close.shift(1)).abs(),
            ],
            axis=1,
        ).max(axis=1)
        features[f"atr_{self.config.atr_window}"] = true_range.rolling(self.config.atr_window).mean()
        features[f"atr_pct_{self.config.atr_window}"] = (
            features[f"atr_{self.config.atr_window}"] / close
        )

        bars_per_year = (
            self.config.bars_per_year_crypto
            if asset_class == AssetClass.CRYPTO
            else self.config.bars_per_year_equity
        )
        vol_window = self.config.volatility_window
        realized_vol = log_returns.rolling(vol_window).std() * np.sqrt(bars_per_year)
        features[f"realized_vol_{vol_window}"] = realized_vol
        features[f"vol_zscore_{vol_window}"] = self._rolling_zscore(realized_vol, vol_window)
        features[f"vol_regime_{vol_window}"] = realized_vol / realized_vol.rolling(vol_window).mean()
        features[f"vol_expansion_{vol_window}"] = features[f"vol_regime_{vol_window}"] - 1.0
        features[f"vol_delta_{vol_window}"] = realized_vol.diff()

        rsi_window = self.config.rsi_window
        delta = close.diff()
        gains = delta.clip(lower=0.0)
        losses = -delta.clip(upper=0.0)
        avg_gain = gains.ewm(alpha=1 / rsi_window, adjust=False, min_periods=rsi_window).mean()
        avg_loss = losses.ewm(alpha=1 / rsi_window, adjust=False, min_periods=rsi_window).mean()
        rs = avg_gain / avg_loss.replace(0.0, np.nan)
        rsi = 100.0 - (100.0 / (1.0 + rs))
        rsi = rsi.mask((avg_loss == 0) & (avg_gain > 0), 100.0)
        rsi = rsi.mask((avg_loss == 0) & (avg_gain == 0), 50.0)
        features[f"rsi_{rsi_window}"] = rsi
        features["rsi_zone"] = np.select(
            [
                features[f"rsi_{rsi_window}"] >= 70,
                features[f"rsi_{rsi_window}"] <= 30,
            ],
            ["overbought", "oversold"],
            default="neutral",
        )

        ma_windows = sorted(set(self.config.moving_average_windows))
        for window in ma_windows:
            moving_average = close.rolling(window).mean()
            features[f"ma_{window}"] = moving_average
            features[f"ma_gap_{window}"] = close / moving_average - 1.0
            features[f"sma_distance_{window}"] = features[f"ma_gap_{window}"]

        if len(ma_windows) >= 2:
            fast = ma_windows[0]
            slow = ma_windows[-1]
            features["trend_spread"] = features[f"ma_{fast}"] / features[f"ma_{slow}"] - 1.0
            features["trend_direction_feature"] = np.select(
                [features["trend_spread"] > 0, features["trend_spread"] < 0],
                ["up", "down"],
                default="neutral",
            )
            features["trend_persistence"] = self._run_length(features["trend_direction_feature"])

        z_window = self.config.zscore_window
        features[f"zscore_close_{z_window}"] = self._rolling_zscore(close, z_window)
        rolling_min = low.rolling(z_window).min()
        rolling_max = high.rolling(z_window).max()
        denominator = (rolling_max - rolling_min).replace(0.0, np.nan)
        features[f"price_position_{z_window}"] = (close - rolling_min) / denominator
        path_length = close.diff().abs().rolling(z_window).sum().replace(0.0, np.nan)
        features[f"range_efficiency_{z_window}"] = close.diff(z_window).abs() / path_length

        volume_window = self.config.volume_window
        rolling_volume = volume.rolling(volume_window).mean()
        rolling_dollar_volume = (close * volume).rolling(volume_window).mean()
        features[f"volume_mean_{volume_window}"] = rolling_volume
        features[f"volume_ratio_{volume_window}"] = volume / rolling_volume
        features[f"volume_zscore_{volume_window}"] = self._rolling_zscore(volume, volume_window)
        features[f"dollar_volume_{volume_window}"] = rolling_dollar_volume
        features[f"volume_regime_{volume_window}"] = np.select(
            [
                features[f"volume_ratio_{volume_window}"] >= 1.25,
                features[f"volume_ratio_{volume_window}"] <= 0.8,
            ],
            ["expansion", "contraction"],
            default="normal",
        )

        return_zscore_window = self.config.return_zscore_window
        for horizon in self.config.return_horizons:
            features[f"return_zscore_{horizon}_{return_zscore_window}"] = self._rolling_zscore(
                features[f"return_{horizon}"],
                return_zscore_window,
            )

        for pair in self.config.acceleration_pairs:
            if len(pair) != 2:
                continue
            fast_horizon, slow_horizon = pair
            fast_rate_column = f"return_rate_{fast_horizon}"
            slow_rate_column = f"return_rate_{slow_horizon}"
            if fast_rate_column in features.columns and slow_rate_column in features.columns:
                features[f"return_accel_{fast_horizon}_{slow_horizon}"] = (
                    features[fast_rate_column] - features[slow_rate_column]
                )

        atr_column = f"atr_{self.config.atr_window}"
        if atr_column in features.columns:
            features["atr_normalized_move_1"] = close.diff() / features[atr_column].replace(0.0, np.nan)
            features["atr_normalized_close_to_sma20"] = (
                (close - features.get("ma_20")) / features[atr_column].replace(0.0, np.nan)
                if "ma_20" in features.columns
                else np.nan
            )
            features["atr_normalized_close_to_sma50"] = (
                (close - features.get("ma_50")) / features[atr_column].replace(0.0, np.nan)
                if "ma_50" in features.columns
                else np.nan
            )
            features["atr_normalized_close_to_sma200"] = (
                (close - features.get("ma_200")) / features[atr_column].replace(0.0, np.nan)
                if "ma_200" in features.columns
                else np.nan
            )

        return features

    @staticmethod
    def _rolling_zscore(series: pd.Series, window: int) -> pd.Series:
        mean = series.rolling(window).mean()
        std = series.rolling(window).std().replace(0.0, np.nan)
        return (series - mean) / std

    @staticmethod
    def _run_length(series: pd.Series) -> pd.Series:
        filled = series.fillna("missing")
        groups = filled.ne(filled.shift()).cumsum()
        return filled.groupby(groups).cumcount() + 1
