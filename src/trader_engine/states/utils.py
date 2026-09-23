from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


def run_length(series: pd.Series) -> pd.Series:
    filled = series.fillna("missing")
    groups = filled.ne(filled.shift()).cumsum()
    return filled.groupby(groups).cumcount() + 1


def bucket_duration(series: pd.Series, thresholds: list[int]) -> pd.Series:
    if not thresholds:
        return pd.Series("age_all", index=series.index)
    ordered = sorted(set(int(value) for value in thresholds))
    labels = []
    previous = 0
    for value in ordered:
        labels.append(f"age_{previous + 1}_{value}")
        previous = value
    labels.append(f"age_{ordered[-1] + 1}_plus")
    bins = [-np.inf, *ordered, np.inf]
    bucketed = pd.cut(series.astype(float), bins=bins, labels=labels, right=True)
    return bucketed.astype("object").fillna("age_unknown")


def infer_bias_from_frame(frame: pd.DataFrame) -> pd.Series:
    if "state_trend" in frame.columns:
        mapping = {"up": "bullish", "down": "bearish", "neutral": "neutral", "mixed": "neutral"}
        return frame["state_trend"].map(mapping).fillna("neutral")

    medium_return = frame["return_20"] if "return_20" in frame.columns else frame.get("return_5", pd.Series(0.0, index=frame.index))
    trend_gap = frame["ma_gap_50"] if "ma_gap_50" in frame.columns else pd.Series(0.0, index=frame.index)
    bullish = (medium_return > 0) & (trend_gap > 0)
    bearish = (medium_return < 0) & (trend_gap < 0)
    return pd.Series(
        np.select([bullish, bearish], ["bullish", "bearish"], default="neutral"),
        index=frame.index,
    )


def sanitize_feature_name(name: str) -> str:
    return name.replace("-", "_").replace(".", "_")


def infer_state_family(frame: pd.DataFrame) -> pd.Series:
    if "state_anchor_regime" in frame.columns:
        return frame["state_anchor_regime"].astype(str)

    if "state_trend" in frame.columns:
        trend = frame["state_trend"].astype(str)
    else:
        medium_return = frame["return_20"] if "return_20" in frame.columns else frame.get("return_5", pd.Series(0.0, index=frame.index))
        trend_gap = frame["ma_gap_50"] if "ma_gap_50" in frame.columns else pd.Series(0.0, index=frame.index)
        trend = pd.Series(
            np.select(
                [(medium_return > 0) & (trend_gap > 0), (medium_return < 0) & (trend_gap < 0)],
                ["up", "down"],
                default="neutral",
            ),
            index=frame.index,
        )

    if "state_volatility" in frame.columns:
        volatility = frame["state_volatility"].astype(str)
    else:
        vol_expansion = frame["vol_expansion_20"] if "vol_expansion_20" in frame.columns else pd.Series(0.0, index=frame.index)
        volatility = pd.Series(
            np.select(
                [vol_expansion > 0.15, vol_expansion < -0.15],
                ["expanding", "compressing"],
                default="stable",
            ),
            index=frame.index,
        )

    return trend.astype(str) + "|" + volatility.astype(str)


@dataclass
class SparseStateReducer:
    component_names: list[str]
    merge_priority: list[str]
    min_observations: int
    merge_label: str = "any"
    rare_label: str = "rare_state"
    _effective_priority: list[str] = field(default_factory=list)
    _counts_by_level: list[pd.Series] = field(default_factory=list)

    def fit(self, component_frame: pd.DataFrame) -> "SparseStateReducer":
        working = self._prepared_components(component_frame)
        self._effective_priority = [column for column in self.merge_priority if column in working.columns]
        self._counts_by_level = []

        current = working.copy()
        self._counts_by_level.append(current.astype(str).agg("|".join, axis=1).value_counts())
        for column in self._effective_priority:
            current = current.copy()
            current[column] = self.merge_label
            self._counts_by_level.append(current.astype(str).agg("|".join, axis=1).value_counts())
        return self

    def transform(self, component_frame: pd.DataFrame) -> pd.Series:
        if not self._counts_by_level:
            return self._prepared_components(component_frame).astype(str).agg("|".join, axis=1)

        working = self._prepared_components(component_frame)
        labels_by_level: list[pd.Series] = []
        current = working.copy()
        labels_by_level.append(current.astype(str).agg("|".join, axis=1))
        for column in self._effective_priority:
            current = current.copy()
            current[column] = self.merge_label
            labels_by_level.append(current.astype(str).agg("|".join, axis=1))

        resolved = pd.Series(self.rare_label, index=working.index, dtype="object")
        for labels, counts in zip(labels_by_level, self._counts_by_level):
            eligible = labels.map(counts).fillna(0).astype(float) >= float(self.min_observations)
            apply_mask = (resolved == self.rare_label) & eligible
            if apply_mask.any():
                resolved.loc[apply_mask] = labels.loc[apply_mask]
        return resolved

    def artifacts(self) -> dict[str, pd.DataFrame]:
        rows = []
        for level, counts in enumerate(self._counts_by_level):
            valid = counts[counts >= self.min_observations]
            rows.append(
                {
                    "level": level,
                    "generalized_components": len(self._effective_priority[:level]),
                    "unique_states": int(len(counts)),
                    "valid_states": int(len(valid)),
                    "share_valid_states": float(len(valid) / max(len(counts), 1)),
                    "observations_in_valid_states": float(valid.sum()),
                }
            )
        return {
            "sparse_merge_levels": pd.DataFrame(rows),
        }

    def _prepared_components(self, component_frame: pd.DataFrame) -> pd.DataFrame:
        if not self.component_names:
            return component_frame.astype(str).copy()
        return component_frame[self.component_names].astype(str).copy()
