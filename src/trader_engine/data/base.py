from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np
import pandas as pd

from trader_engine.core.models import DataRequest, UniverseMember


class HistoricalDataProvider(ABC):
    name: str

    @abstractmethod
    def fetch_bars(self, request: DataRequest) -> pd.DataFrame:
        raise NotImplementedError


class LiveDataProvider(ABC):
    name: str

    @abstractmethod
    def fetch_latest_bar(self, member: UniverseMember, interval: str) -> pd.Series:
        raise NotImplementedError


REQUIRED_BAR_COLUMNS = ("open", "high", "low", "close", "volume")


def normalize_ohlcv(frame: pd.DataFrame) -> pd.DataFrame:
    normalized = frame.copy()
    if isinstance(normalized.columns, pd.MultiIndex):
        normalized.columns = normalized.columns.get_level_values(0)
    normalized.columns = [str(column).lower().replace(" ", "_") for column in normalized.columns]
    if "adj_close" in normalized.columns and "close" not in normalized.columns:
        normalized["close"] = normalized["adj_close"]
    missing = [column for column in REQUIRED_BAR_COLUMNS if column not in normalized.columns]
    if missing:
        raise ValueError(f"Market data missing required columns: {missing}")
    normalized = normalized[list(REQUIRED_BAR_COLUMNS)].sort_index()
    if getattr(normalized.index, "tz", None) is not None:
        normalized.index = normalized.index.tz_convert("UTC").tz_localize(None)
    normalized = normalized.apply(pd.to_numeric, errors="raise")
    validate_bars(normalized)
    return normalized


def validate_bars(frame: pd.DataFrame, require_volume: bool = True) -> None:
    """Reject malformed bars instead of silently changing the research sample."""
    required = list(REQUIRED_BAR_COLUMNS if require_volume else REQUIRED_BAR_COLUMNS[:4])
    if not frame.columns.is_unique or any(column not in frame for column in required):
        raise ValueError("Bars require unique OHLC columns and volume for ingestion")
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.hasnans or not frame.index.is_unique:
        raise ValueError("Bars require unique, nonmissing datetime timestamps")
    values = frame[required].apply(pd.to_numeric, errors="coerce")
    if not np.isfinite(values.to_numpy(dtype=float)).all():
        raise ValueError("Bars must contain finite numeric values")
    prices = values[["open", "high", "low", "close"]]
    if (prices <= 0).any().any():
        raise ValueError("OHLC prices must be positive")
    if ((prices["high"] < prices[["open", "close", "low"]].max(axis=1)) |
            (prices["low"] > prices[["open", "close", "high"]].min(axis=1))).any():
        raise ValueError("Inconsistent OHLC range")
    if "volume" in frame:
        volume = pd.to_numeric(frame["volume"], errors="coerce")
        if not np.isfinite(volume.to_numpy(dtype=float)).all() or (volume < 0).any():
            raise ValueError("Volume must be finite and nonnegative")
