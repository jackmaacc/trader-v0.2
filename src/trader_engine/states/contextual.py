from __future__ import annotations

import pandas as pd

from trader_engine.core.config import StateConfig
from trader_engine.states.base import BaseStateModel
from trader_engine.states.utils import bucket_duration, infer_bias_from_frame, run_length


class ContextualStateModel(BaseStateModel):
    def __init__(self, base_model: BaseStateModel, state_config: StateConfig) -> None:
        self.base_model = base_model
        self.state_config = state_config

    def fit(self, frame: pd.DataFrame) -> "ContextualStateModel":
        self.base_model.fit(frame)
        return self

    def classify(self, frame: pd.DataFrame) -> pd.DataFrame:
        classified = self.base_model.classify(frame).copy()
        if "state" not in classified.columns:
            raise ValueError("Base state model did not produce a `state` column.")

        classified["base_state"] = classified["state"].astype(str)
        classified["previous_state"] = classified["base_state"].shift(1).fillna("start")
        classified["state_age"] = run_length(classified["base_state"])
        classified["state_age_bucket"] = bucket_duration(classified["state_age"], self.state_config.duration_buckets)

        bias = infer_bias_from_frame(classified)
        classified["state_bias"] = bias
        classified["previous_state_group"] = bias.shift(1).fillna("start")
        classified["recent_state_path"] = self._recent_path(classified)
        classified["entry_bias"] = self._entry_bias(classified)

        contextual_columns = ["base_state"]
        if self.state_config.append_previous_state:
            if self.state_config.previous_state_mode == "full":
                contextual_columns.append("previous_state")
            else:
                contextual_columns.append("previous_state_group")
        if self.state_config.append_state_age_bucket:
            contextual_columns.append("state_age_bucket")
        if self.state_config.append_recent_path:
            contextual_columns.append("recent_state_path")
        if self.state_config.append_entry_bias:
            contextual_columns.append("entry_bias")

        classified["state"] = classified[contextual_columns].astype(str).agg("|".join, axis=1)
        return classified

    def model_artifacts(self) -> dict[str, pd.DataFrame | dict]:
        artifacts = self.base_model.model_artifacts()
        artifacts["context_settings"] = pd.DataFrame(
            [
                {"setting": "append_previous_state", "value": self.state_config.append_previous_state},
                {"setting": "previous_state_mode", "value": self.state_config.previous_state_mode},
                {"setting": "append_state_age_bucket", "value": self.state_config.append_state_age_bucket},
                {"setting": "duration_buckets", "value": str(self.state_config.duration_buckets)},
                {"setting": "append_recent_path", "value": self.state_config.append_recent_path},
                {"setting": "recent_path_length", "value": self.state_config.recent_path_length},
                {"setting": "append_entry_bias", "value": self.state_config.append_entry_bias},
            ]
        )
        return artifacts

    def _recent_path(self, classified: pd.DataFrame) -> pd.Series:
        length = max(int(self.state_config.recent_path_length), 1)
        if self.state_config.previous_state_mode == "full":
            source = classified["base_state"]
        else:
            source = classified["state_bias"]
        parts = []
        for lag in range(length, 0, -1):
            parts.append(source.shift(lag).fillna("start"))
        return pd.concat(parts, axis=1).astype(str).agg(">".join, axis=1)

    @staticmethod
    def _entry_bias(classified: pd.DataFrame) -> pd.Series:
        groups = classified["base_state"].ne(classified["base_state"].shift()).cumsum()
        run_starts = groups.ne(groups.shift())
        entered_from = classified["previous_state_group"].where(run_starts)
        return entered_from.groupby(groups).transform("first").fillna("start")
