from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from trader_engine.markov.engine import MarkovAnalysisResult


@dataclass
class StateDiagnosticsResult:
    state_frequency: pd.DataFrame
    state_persistence: pd.DataFrame
    state_quality: pd.DataFrame
    transition_quality: pd.DataFrame
    state_tradability: pd.DataFrame
    sparse_states: pd.DataFrame
    summary: dict[str, float | int]


def compute_state_diagnostics(
    frame: pd.DataFrame,
    analysis: MarkovAnalysisResult,
    sparse_threshold: int,
    trades: pd.DataFrame | None = None,
) -> StateDiagnosticsResult:
    state_series = frame["state"].dropna().astype(str)
    state_frequency = (
        state_series.value_counts()
        .rename_axis("state")
        .reset_index(name="observations")
        .assign(frequency=lambda data: data["observations"] / max(len(state_series), 1))
    )
    state_frequency_detail = state_frequency.rename(columns={"observations": "state_occupancy_observations"})

    persistence = _state_persistence(frame)
    state_quality = analysis.state_returns.reset_index().rename(columns={"state": "state"})
    if not state_quality.empty:
        state_quality = state_quality.rename(columns={state_quality.columns[0]: "state"})
        state_quality = state_quality.merge(state_frequency_detail, on="state", how="left")
        state_quality["transition_entropy"] = state_quality["state"].map(analysis.state_entropy).fillna(1.0)
        state_quality["transition_confidence"] = state_quality["state"].map(analysis.state_confidence).fillna(0.0)
        transition_concentration = (
            analysis.transition_matrix.max(axis=1) if not analysis.transition_matrix.empty else pd.Series(dtype=float)
        )
        state_quality["transition_concentration"] = state_quality["state"].map(transition_concentration).fillna(0.0)
        sample_size = state_quality["observations"] if "observations" in state_quality.columns else 0
        state_quality["sparse_warning"] = sample_size < sparse_threshold

    transition_quality = analysis.transition_returns.copy()
    if not transition_quality.empty and not analysis.transition_matrix.empty:
        probabilities = analysis.transition_matrix.stack().rename("transition_probability").reset_index()
        probabilities.columns = ["current_state", "next_state", "transition_probability"]
        counts = analysis.transition_counts.stack().rename("transition_count").reset_index()
        counts.columns = ["current_state", "next_state", "transition_count"]
        transition_quality = transition_quality.merge(probabilities, on=["current_state", "next_state"], how="left")
        transition_quality = transition_quality.merge(counts, on=["current_state", "next_state"], how="left")

    sparse_states = state_frequency.loc[state_frequency["observations"] < sparse_threshold].copy()
    state_tradability = _state_tradability(state_frequency, trades)

    weighted_stability = 0.0
    weighted_concentration = 0.0
    if not state_quality.empty:
        weights = (
            state_quality["observations"].fillna(0.0)
            if "observations" in state_quality.columns
            else state_quality["state_occupancy_observations"].fillna(0.0)
        )
        if weights.sum() > 0:
            weighted_stability = float(
                (1.0 - state_quality["transition_entropy"].fillna(1.0)).mul(weights).sum() / weights.sum()
            )
            weighted_concentration = float(
                state_quality["transition_concentration"].fillna(0.0).mul(weights).sum() / weights.sum()
            )

    above_threshold = state_frequency["observations"] >= sparse_threshold if not state_frequency.empty else pd.Series(dtype=bool)
    effective_coverage = (
        float(state_frequency.loc[above_threshold, "observations"].sum() / max(state_frequency["observations"].sum(), 1))
        if not state_frequency.empty
        else 0.0
    )
    trade_generating_percentage = (
        float(state_tradability["trade_generating"].mean()) if not state_tradability.empty else 0.0
    )
    median_observations = float(state_frequency["observations"].median()) if not state_frequency.empty else 0.0
    min_observations = int(state_frequency["observations"].min()) if not state_frequency.empty else 0

    summary = {
        "unique_states": int(state_frequency["state"].nunique()) if not state_frequency.empty else 0,
        "median_state_observations": median_observations,
        "min_state_observations": min_observations,
        "share_states_above_threshold": float(above_threshold.mean()) if not state_frequency.empty else 0.0,
        "sparse_state_count": int(len(sparse_states)),
        "sparse_state_ratio": float(len(sparse_states) / max(len(state_frequency), 1)),
        "effective_state_coverage": effective_coverage,
        "transition_stability": weighted_stability,
        "transition_concentration": weighted_concentration,
        "mean_state_persistence": float(persistence["mean_duration"].mean()) if not persistence.empty else 0.0,
        "trade_generating_state_percentage": trade_generating_percentage,
        "out_of_sample_trade_count": int(len(trades)) if trades is not None else 0,
    }
    return StateDiagnosticsResult(
        state_frequency=state_frequency,
        state_persistence=persistence,
        state_quality=state_quality,
        transition_quality=transition_quality,
        state_tradability=state_tradability,
        sparse_states=sparse_states,
        summary=summary,
    )


def _state_persistence(frame: pd.DataFrame) -> pd.DataFrame:
    state_series = frame["state"].dropna().astype(str)
    if state_series.empty:
        return pd.DataFrame()
    groups = state_series.ne(state_series.shift()).cumsum()
    runs = state_series.groupby(groups).agg(state="first", duration="size").reset_index(drop=True)
    persistence = (
        runs.groupby("state")["duration"]
        .agg(run_count="count", mean_duration="mean", median_duration="median", max_duration="max")
        .reset_index()
        .sort_values("mean_duration", ascending=False)
    )
    return persistence


def _state_tradability(state_frequency: pd.DataFrame, trades: pd.DataFrame | None) -> pd.DataFrame:
    tradability = state_frequency.copy()
    if tradability.empty:
        return tradability
    if trades is None or trades.empty or "entry_state" not in trades.columns:
        tradability["trade_count"] = 0
        tradability["trade_generating"] = False
        tradability["mean_trade_return"] = 0.0
        tradability["mean_expected_value"] = 0.0
        return tradability

    trade_summary = (
        trades.groupby("entry_state")
        .agg(
            trade_count=("entry_state", "size"),
            mean_trade_return=("return_pct", "mean"),
            mean_expected_value=("entry_expected_value", "mean"),
        )
        .reset_index()
        .rename(columns={"entry_state": "state"})
    )
    tradability = tradability.merge(trade_summary, on="state", how="left")
    tradability["trade_count"] = tradability["trade_count"].fillna(0).astype(int)
    tradability["trade_generating"] = tradability["trade_count"] > 0
    tradability["mean_trade_return"] = tradability["mean_trade_return"].fillna(0.0)
    tradability["mean_expected_value"] = tradability["mean_expected_value"].fillna(0.0)
    return tradability
