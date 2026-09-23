from __future__ import annotations

from abc import ABC, abstractmethod
from copy import deepcopy
from typing import Any

import pandas as pd


class BaseStateModel(ABC):
    def fit(self, frame: pd.DataFrame) -> "BaseStateModel":
        return self

    @abstractmethod
    def classify(self, frame: pd.DataFrame) -> pd.DataFrame:
        raise NotImplementedError

    def fit_transform(self, frame: pd.DataFrame) -> pd.DataFrame:
        self.fit(frame)
        return self.classify(frame)

    def clone(self) -> "BaseStateModel":
        return deepcopy(self)

    def model_artifacts(self) -> dict[str, pd.DataFrame | dict[str, Any]]:
        return {}
