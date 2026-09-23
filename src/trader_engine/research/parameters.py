from __future__ import annotations

from itertools import product
from typing import Any

from trader_engine.core.config import AppConfig


def generate_parameter_grid(
    search_space: dict[str, list[bool | int | float | str]],
    max_combinations: int,
) -> list[dict[str, Any]]:
    if not search_space:
        return []

    items = list(search_space.items())
    combination_count = 1
    for _, values in items:
        combination_count *= max(len(values), 1)

    if combination_count > max_combinations:
        raise ValueError(
            f"Search space expands to {combination_count} combinations, which exceeds max_combinations={max_combinations}."
        )

    return [
        {path: value for (path, _), value in zip(items, combination)}
        for combination in product(*(values for _, values in items))
    ]


def apply_parameter_overrides(config: AppConfig, overrides: dict[str, Any]) -> AppConfig:
    payload = config.model_dump(mode="python")
    for path, value in overrides.items():
        _set_nested(payload, path.split("."), value)
    return AppConfig.model_validate(payload)


def _set_nested(payload: dict[str, Any], parts: list[str], value: Any) -> None:
    current = payload
    for index, part in enumerate(parts):
        is_last = index == len(parts) - 1
        if is_last:
            current[part] = value
            return
        if part not in current or current[part] is None:
            current[part] = {}
        current = current[part]
