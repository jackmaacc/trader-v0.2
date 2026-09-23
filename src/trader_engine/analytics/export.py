from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd


class ArtifactStore:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def write_frame(self, relative_path: str, frame: pd.DataFrame) -> Path:
        path = self.base_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        include_index = not isinstance(frame.index, pd.RangeIndex)
        if path.suffix == ".parquet":
            frame.to_parquet(path)
        elif path.suffix == ".csv":
            frame.to_csv(path, index=include_index)
        else:
            raise ValueError(f"Unsupported frame artifact type: {path.suffix}")
        return path

    def write_json(self, relative_path: str, payload: dict[str, Any]) -> Path:
        path = self.base_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, default=str)
        return path
