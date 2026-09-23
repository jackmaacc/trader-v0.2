from __future__ import annotations

from pathlib import Path

import pandas as pd

from trader_engine.core.models import DataRequest


class MarketDataCache:
    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, request: DataRequest) -> Path:
        filename = f"{request.member.symbol}_{request.interval}_{request.cache_key()}.parquet"
        return self.cache_dir / filename

    def load(self, request: DataRequest) -> pd.DataFrame | None:
        path = self._path_for(request)
        if not path.exists():
            return None
        frame = pd.read_parquet(path)
        frame.index = pd.to_datetime(frame.index)
        return frame.sort_index()

    def store(self, request: DataRequest, frame: pd.DataFrame) -> None:
        path = self._path_for(request)
        frame.sort_index().to_parquet(path)
