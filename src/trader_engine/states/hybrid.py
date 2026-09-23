from __future__ import annotations

import numpy as np
import pandas as pd

from trader_engine.core.config import FeatureConfig, StateConfig
from trader_engine.states.base import BaseStateModel
from trader_engine.states.clustering import ClusterBundle, fit_cluster_bundle, predict_cluster_ids


class HybridRegimeKMeansStateModel(BaseStateModel):
    def __init__(self, state_config: StateConfig, feature_config: FeatureConfig) -> None:
        self.state_config = state_config
        self.feature_config = feature_config
        self.bundle_by_regime: dict[str, ClusterBundle] = {}
        self.regime_sizes: pd.Series = pd.Series(dtype=float)

    def fit(self, frame: pd.DataFrame) -> "HybridRegimeKMeansStateModel":
        regimes = self._coarse_regime(frame)
        self.bundle_by_regime = {}
        self.regime_sizes = regimes.value_counts().sort_index()
        requested_cluster_count = self.state_config.hybrid_cluster_count or self.state_config.cluster_count

        for regime, regime_frame in frame.groupby(regimes):
            if len(regime_frame) < self.state_config.hybrid_min_regime_samples:
                continue
            self.bundle_by_regime[str(regime)] = fit_cluster_bundle(
                frame=regime_frame,
                state_config=self.state_config,
                selected_features=self.state_config.cluster_features,
                requested_cluster_count=requested_cluster_count,
            )
        return self

    def classify(self, frame: pd.DataFrame) -> pd.DataFrame:
        classified = frame.copy()
        regimes = self._coarse_regime(frame)
        classified["state_anchor_regime"] = regimes
        classified["state_cluster"] = "cluster_00"
        for regime, bundle in self.bundle_by_regime.items():
            mask = regimes == regime
            if not mask.any():
                continue
            cluster_ids = predict_cluster_ids(frame=frame.loc[mask], bundle=bundle)
            classified.loc[mask, "state_cluster"] = [f"cluster_{cluster:02d}" for cluster in cluster_ids]
        classified["state"] = classified["state_anchor_regime"].astype(str) + "|" + classified["state_cluster"].astype(str)
        classified["state_family"] = classified["state_anchor_regime"].astype(str)
        return classified

    def model_artifacts(self) -> dict[str, pd.DataFrame | dict]:
        regime_summary = self.regime_sizes.rename_axis("regime").reset_index(name="observations")
        regime_summary["has_local_cluster_model"] = regime_summary["regime"].map(
            lambda regime: regime in self.bundle_by_regime
        )

        center_frames = []
        size_frames = []
        settings_rows = [
            {"parameter": "model_name", "value": self.state_config.model_name},
            {"parameter": "hybrid_anchor", "value": self.state_config.hybrid_anchor},
            {"parameter": "hybrid_cluster_count", "value": self.state_config.hybrid_cluster_count or self.state_config.cluster_count},
            {"parameter": "hybrid_min_regime_samples", "value": self.state_config.hybrid_min_regime_samples},
        ]
        for regime, bundle in self.bundle_by_regime.items():
            centers = bundle.cluster_centers_original.copy()
            centers.insert(0, "regime", regime)
            center_frames.append(centers)

            sizes = (
                bundle.cluster_sizes.rename_axis("cluster_id")
                .reset_index(name="observations")
                .assign(regime=regime)
                .assign(cluster=lambda data: data["cluster_id"].map(lambda value: f"cluster_{int(value):02d}"))
            )
            size_frames.append(sizes[["regime", "cluster", "observations"]])
            settings_rows.append({"parameter": f"{regime}_cluster_count", "value": len(bundle.cluster_sizes)})

        artifacts: dict[str, pd.DataFrame | dict] = {
            "hybrid_regime_summary": regime_summary,
            "hybrid_model_settings": pd.DataFrame(settings_rows),
        }
        if center_frames:
            artifacts["hybrid_cluster_centers"] = pd.concat(center_frames, ignore_index=True)
        if size_frames:
            artifacts["hybrid_cluster_sizes"] = pd.concat(size_frames, ignore_index=True)
        return artifacts

    def _coarse_regime(self, frame: pd.DataFrame) -> pd.Series:
        if self.state_config.hybrid_anchor == "volatility":
            vol_window = self.feature_config.volatility_window
            vol_expansion = frame[f"vol_expansion_{vol_window}"] if f"vol_expansion_{vol_window}" in frame.columns else pd.Series(0.0, index=frame.index)
            vol_zscore = frame[f"vol_zscore_{vol_window}"] if f"vol_zscore_{vol_window}" in frame.columns else pd.Series(0.0, index=frame.index)
            return pd.Series(
                np.select(
                    [
                        (vol_expansion > self.state_config.vol_regime_threshold)
                        | (vol_zscore > self.state_config.volatility_zscore_threshold),
                        (vol_expansion < -self.state_config.vol_regime_threshold)
                        | (vol_zscore < -self.state_config.volatility_zscore_threshold),
                    ],
                    ["volatile", "calm"],
                    default="stable",
                ),
                index=frame.index,
            )

        medium = self.state_config.medium_horizon
        medium_return = frame[f"return_{medium}"] if f"return_{medium}" in frame.columns else frame.get("return_20", pd.Series(0.0, index=frame.index))
        ma_fast = frame["ma_gap_20"] if "ma_gap_20" in frame.columns else pd.Series(0.0, index=frame.index)
        ma_slow = frame["ma_gap_200"] if "ma_gap_200" in frame.columns else frame.get("ma_gap_50", pd.Series(0.0, index=frame.index))
        bullish = (medium_return > self.state_config.trend_threshold) & (ma_fast > 0) & (ma_slow > 0)
        bearish = (medium_return < -self.state_config.trend_threshold) & (ma_fast < 0) & (ma_slow < 0)
        return pd.Series(np.select([bullish, bearish], ["uptrend", "downtrend"], default="neutral"), index=frame.index)
