from __future__ import annotations

import pandas as pd

from trader_engine.analytics.quality import EdgeQualityResult, edge_quality
from trader_engine.signals.engine import QualityGateTables


def build_quality_gate_tables(
    trades: pd.DataFrame,
    min_group_observations: int = 1,
) -> tuple[QualityGateTables, dict[str, EdgeQualityResult]]:
    if trades.empty:
        return QualityGateTables(), {}

    state_groups = _quality_groups(trades, "entry_state")
    transition_groups = _quality_groups(trades, "entry_transition_setup")
    family_groups = _quality_groups(trades, "entry_state_family")
    state_result = edge_quality(trades, state_groups, min_group_observations=min_group_observations)
    transition_result = edge_quality(trades, transition_groups, min_group_observations=min_group_observations)
    family_result = (
        edge_quality(trades, family_groups, min_group_observations=min_group_observations)
        if "entry_state_family" in trades.columns
        else EdgeQualityResult(summary=pd.DataFrame(), fold_consistency=pd.DataFrame())
    )

    state_table = state_result.summary.rename(columns={"entry_state": "state"})
    transition_table = transition_result.summary.rename(columns={"entry_transition_setup": "transition_setup"})
    family_table = family_result.summary.rename(columns={"entry_state_family": "state_family"})
    return (
        QualityGateTables(
            state_quality=state_table,
            transition_quality=transition_table,
            state_family_quality=family_table,
        ),
        {"state": state_result, "transition": transition_result, "family": family_result},
    )


def quality_report_frames(
    trades: pd.DataFrame,
    min_group_observations: int = 1,
    top_n: int = 20,
) -> dict[str, pd.DataFrame]:
    if trades.empty:
        return {}

    gate_tables, results = build_quality_gate_tables(trades, min_group_observations=min_group_observations)
    family_result = results.get("family", EdgeQualityResult(summary=pd.DataFrame(), fold_consistency=pd.DataFrame()))
    state_summary = gate_tables.state_quality
    transition_summary = gate_tables.transition_quality
    frames = {
        "state_quality_summary": state_summary,
        "transition_quality_summary": transition_summary,
        "state_fold_consistency": results["state"].fold_consistency,
        "transition_fold_consistency": results["transition"].fold_consistency,
        "state_family_performance": family_result.summary,
        "state_family_fold_consistency": family_result.fold_consistency,
        "best_states": _top_rows(state_summary, top_n=top_n),
        "worst_states": _bottom_rows(state_summary, top_n=top_n),
        "best_transitions": _top_rows(transition_summary, top_n=top_n),
        "worst_transitions": _bottom_rows(transition_summary, top_n=top_n),
    }
    return frames


def _quality_groups(trades: pd.DataFrame, terminal_column: str) -> list[str]:
    groups: list[str] = []
    if "asset_class" in trades.columns:
        groups.append("asset_class")
    groups.append(terminal_column)
    return groups


def _top_rows(frame: pd.DataFrame, top_n: int) -> pd.DataFrame:
    if frame.empty:
        return frame
    if {"quality_score", "expectancy"}.issubset(frame.columns):
        return frame.sort_values(["quality_score", "expectancy"], ascending=[False, False]).head(top_n)
    return frame.head(top_n)


def _bottom_rows(frame: pd.DataFrame, top_n: int) -> pd.DataFrame:
    if frame.empty:
        return frame
    if {"quality_score", "expectancy"}.issubset(frame.columns):
        return frame.sort_values(["quality_score", "expectancy"], ascending=[True, True]).head(top_n)
    return frame.tail(top_n)
