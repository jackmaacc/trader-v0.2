from __future__ import annotations

import numpy as np
import pandas as pd

from trader_engine.core.config import StateConfig
from trader_engine.states.base import BaseStateModel
from trader_engine.states.utils import SparseStateReducer, infer_state_family, sanitize_feature_name


class FeatureBinStateModel(BaseStateModel):
    def __init__(self, state_config: StateConfig) -> None:
        self.state_config = state_config
        self.selected_features: list[str] = []
        self.bin_edges: dict[str, np.ndarray] = {}
        self.active_groups: list[dict[str, object]] = []
        self.group_stats: dict[str, dict[str, tuple[float, float]]] = {}
        self.sparse_reducer: SparseStateReducer | None = None

    def fit(self, frame: pd.DataFrame) -> "FeatureBinStateModel":
        prepared = self._prepare_feature_frame(frame, fit=True)
        self.selected_features = self._select_features(prepared)
        if not self.selected_features:
            raise ValueError("No configured bin_features were found in the feature frame.")

        self.bin_edges = {}
        for feature in self.selected_features:
            series = prepared[feature].dropna()
            if series.empty:
                self.bin_edges[feature] = np.array([-np.inf, np.inf])
                continue
            bin_count = max(int(self.state_config.bin_feature_bin_counts.get(feature, self.state_config.bin_count)), 2)
            if self.state_config.binning_strategy == "quantile":
                quantiles = np.linspace(0.0, 1.0, bin_count + 1)
                edges = np.quantile(series.to_numpy(), quantiles)
            else:
                edges = np.linspace(series.min(), series.max(), bin_count + 1)
            edges = np.unique(edges)
            if len(edges) <= 1:
                edges = np.array([series.min() - 1.0, series.max() + 1.0])
            edges[0] = -np.inf
            edges[-1] = np.inf
            self.bin_edges[feature] = edges

        if self.state_config.bin_merge_sparse_states:
            component_frame = self._component_frame(prepared)
            threshold = self.state_config.bin_min_state_observations or self.state_config.sparse_state_warning_threshold
            merge_priority = self._merge_priority(component_frame.columns.tolist())
            self.sparse_reducer = SparseStateReducer(
                component_names=component_frame.columns.tolist(),
                merge_priority=merge_priority,
                min_observations=threshold,
                merge_label=self.state_config.bin_merge_label,
            ).fit(component_frame)
        else:
            self.sparse_reducer = None
        return self

    def classify(self, frame: pd.DataFrame) -> pd.DataFrame:
        if not self.bin_edges:
            raise ValueError("FeatureBinStateModel must be fitted before classify().")
        prepared = self._prepare_feature_frame(frame, fit=False)
        classified = frame.copy()
        for group in self.active_groups:
            column_name = str(group["column_name"])
            classified[column_name] = prepared[column_name]

        component_frame = self._component_frame(prepared)
        for column_name in component_frame.columns:
            classified[column_name] = component_frame[column_name]

        if self.sparse_reducer is not None:
            classified["state"] = self.sparse_reducer.transform(component_frame)
        else:
            classified["state"] = component_frame.astype(str).agg("|".join, axis=1)
        classified["state_family"] = infer_state_family(prepared)
        return classified

    def model_artifacts(self) -> dict[str, pd.DataFrame | dict]:
        bin_rows = []
        for feature, edges in self.bin_edges.items():
            bin_rows.append(
                {
                    "feature": feature,
                    "bin_count": len(edges) - 1,
                    "edges": ", ".join(f"{edge:.6g}" for edge in edges),
                }
            )
        artifacts: dict[str, pd.DataFrame | dict] = {"bin_edges": pd.DataFrame(bin_rows)}
        if self.active_groups:
            artifacts["feature_groups"] = pd.DataFrame(self.active_groups)
        if self.sparse_reducer is not None:
            artifacts.update(self.sparse_reducer.artifacts())
        return artifacts

    def _prepare_feature_frame(self, frame: pd.DataFrame, fit: bool) -> pd.DataFrame:
        prepared = frame.copy()
        if fit:
            self.active_groups = []
            self.group_stats = {}

        for group in self.state_config.bin_feature_groups:
            column_name = f"bin_group_{sanitize_feature_name(group.name)}"
            available = [feature for feature in group.features if feature in prepared.columns]
            if not available:
                continue
            if fit:
                self.active_groups.append(
                    {
                        "group_name": group.name,
                        "column_name": column_name,
                        "features": ",".join(available),
                        "reducer": group.reducer,
                        "replace_members": group.replace_members,
                    }
                )
                self.group_stats[column_name] = {}

            group_series = []
            for feature in available:
                series = prepared[feature].astype(float)
                if group.reducer == "mean_zscore":
                    if fit:
                        mean = float(series.mean()) if series.notna().any() else 0.0
                        std = float(series.std()) if series.notna().any() else 0.0
                        self.group_stats[column_name][feature] = (mean, std)
                    mean, std = self.group_stats.get(column_name, {}).get(feature, (0.0, 0.0))
                    if std == 0.0 or np.isnan(std):
                        transformed = series - mean
                    else:
                        transformed = (series - mean) / std
                else:
                    transformed = series
                group_series.append(transformed)
            prepared[column_name] = pd.concat(group_series, axis=1).mean(axis=1)
        return prepared

    def _select_features(self, prepared: pd.DataFrame) -> list[str]:
        disabled = set(self.state_config.bin_disabled_features)
        group_columns = {str(group["column_name"]): group for group in self.active_groups}
        replaced_members = {
            feature
            for group in self.active_groups
            if bool(group["replace_members"])
            for feature in str(group["features"]).split(",")
            if feature
        }

        selected: list[str] = []
        for feature in self.state_config.bin_features:
            if feature in disabled or feature in replaced_members:
                continue
            if feature in prepared.columns and prepared[feature].notna().any():
                selected.append(feature)

        for column_name in group_columns:
            if column_name in disabled:
                continue
            if column_name in prepared.columns and prepared[column_name].notna().any() and column_name not in selected:
                selected.append(column_name)
        return selected

    def _component_frame(self, prepared: pd.DataFrame) -> pd.DataFrame:
        component_frame = pd.DataFrame(index=prepared.index)
        for feature in self.selected_features:
            edges = self.bin_edges[feature]
            labels = [f"b{index}" for index in range(len(edges) - 1)]
            column_name = f"state_feature_{sanitize_feature_name(feature)}"
            component_frame[column_name] = pd.cut(
                prepared[feature],
                bins=edges,
                labels=labels,
                include_lowest=True,
            ).astype("object").fillna("missing")
        return component_frame

    def _merge_priority(self, component_columns: list[str]) -> list[str]:
        configured = [f"state_feature_{sanitize_feature_name(name)}" for name in self.state_config.bin_merge_order]
        priority = [column for column in configured if column in component_columns]
        if priority:
            remaining = [column for column in reversed(component_columns) if column not in priority]
            return priority + remaining
        return list(reversed(component_columns))
