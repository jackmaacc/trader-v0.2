from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from trader_engine.core.config import MarkovConfig


@dataclass
class MarkovAnalysisResult:
    transition_counts: pd.DataFrame
    transition_matrix: pd.DataFrame
    state_returns: pd.DataFrame
    transition_returns: pd.DataFrame
    state_confidence: pd.Series
    state_entropy: pd.Series
    confusion_matrix: pd.DataFrame
    accuracy: float

    def distribution_for(self, state: str) -> pd.Series:
        if state not in self.transition_matrix.index:
            return pd.Series(dtype=float)
        return self.transition_matrix.loc[state].sort_values(ascending=False)

    def stats_for(self, state: str) -> pd.Series:
        if state not in self.state_returns.index:
            return pd.Series(dtype=float)
        return self.state_returns.loc[state]


class MarkovAnalyzer:
    def __init__(self, config: MarkovConfig) -> None:
        self.config = config

    def analyze(self, frame: pd.DataFrame, state_column: str = "state", price_column: str = "close") -> MarkovAnalysisResult:
        working = frame[[state_column, price_column]].copy()
        working = working.dropna(subset=[state_column, price_column])
        if self.config.transition_lookback_bars and len(working) > self.config.transition_lookback_bars:
            working = working.iloc[-self.config.transition_lookback_bars :].copy()
        working["next_state"] = working[state_column].shift(-self.config.transition_horizon_bars)
        working["forward_return"] = (
            working[price_column].shift(-self.config.forward_return_horizon_bars) / working[price_column] - 1.0
        )

        transition_samples = working.dropna(subset=["next_state"])
        states = sorted(
            set(transition_samples[state_column].unique()).union(set(transition_samples["next_state"].unique()))
        )
        if states:
            transition_counts = pd.crosstab(
                transition_samples[state_column],
                transition_samples["next_state"],
            ).reindex(index=states, columns=states, fill_value=0)
            smoothed = transition_counts.astype(float) + self.config.laplace_smoothing
            transition_matrix = smoothed.div(smoothed.sum(axis=1), axis=0)
        else:
            transition_counts = pd.DataFrame()
            transition_matrix = pd.DataFrame()

        state_return_source = working.dropna(subset=["forward_return"])
        if state_return_source.empty:
            state_returns = pd.DataFrame(
                columns=[
                    "mean_return",
                    "median_return",
                    "std_return",
                    "conditional_sharpe",
                    "positive_rate",
                    "avg_win",
                    "avg_loss",
                    "expectancy",
                    "observations",
                ]
            )
            transition_returns = pd.DataFrame(
                columns=[
                    "current_state",
                    "next_state",
                    "mean_return",
                    "median_return",
                    "std_return",
                    "conditional_sharpe",
                    "positive_rate",
                    "expectancy",
                    "observations",
                ]
            )
        else:
            state_returns = state_return_source.groupby(state_column)["forward_return"].agg(
                mean_return="mean",
                median_return="median",
                std_return="std",
                conditional_sharpe=lambda series: _safe_sharpe(series),
                positive_rate=lambda series: (series > 0).mean(),
                avg_win=lambda series: series[series > 0].mean() if (series > 0).any() else 0.0,
                avg_loss=lambda series: series[series < 0].mean() if (series < 0).any() else 0.0,
                expectancy="mean",
                observations="count",
            )
            transition_returns = (
                state_return_source.dropna(subset=["next_state"])
                .groupby([state_column, "next_state"])["forward_return"]
                .agg(
                    mean_return="mean",
                    median_return="median",
                    std_return="std",
                    conditional_sharpe=lambda series: _safe_sharpe(series),
                    positive_rate=lambda series: (series > 0).mean(),
                    expectancy="mean",
                    observations="count",
                )
                .reset_index()
                .rename(columns={state_column: "current_state"})
            )

        state_entropy = self._row_entropy(transition_matrix)
        state_confidence = (1.0 - state_entropy).clip(lower=0.0)
        confusion_matrix, accuracy = self._prediction_diagnostics(transition_samples, transition_matrix, state_column)

        return MarkovAnalysisResult(
            transition_counts=transition_counts,
            transition_matrix=transition_matrix,
            state_returns=state_returns,
            transition_returns=transition_returns,
            state_confidence=state_confidence,
            state_entropy=state_entropy,
            confusion_matrix=confusion_matrix,
            accuracy=accuracy,
        )

    @staticmethod
    def _row_entropy(matrix: pd.DataFrame) -> pd.Series:
        if matrix.empty:
            return pd.Series(dtype=float)

        def entropy(row: pd.Series) -> float:
            probabilities = row[row > 0].to_numpy()
            if probabilities.size <= 1:
                return 0.0
            raw = -(probabilities * np.log(probabilities)).sum()
            return raw / np.log(probabilities.size)

        return matrix.apply(entropy, axis=1)

    @staticmethod
    def _prediction_diagnostics(
        samples: pd.DataFrame,
        transition_matrix: pd.DataFrame,
        state_column: str,
    ) -> tuple[pd.DataFrame, float]:
        if samples.empty or transition_matrix.empty:
            return pd.DataFrame(), 0.0

        dominant_next_state = transition_matrix.idxmax(axis=1).to_dict()
        predicted = samples[state_column].map(dominant_next_state)
        actual = samples["next_state"]
        diagnostics = pd.DataFrame({"predicted": predicted, "actual": actual}).dropna()
        if diagnostics.empty:
            return pd.DataFrame(), 0.0
        confusion = pd.crosstab(diagnostics["predicted"], diagnostics["actual"])
        accuracy = float((diagnostics["predicted"] == diagnostics["actual"]).mean())
        return confusion, accuracy


def _safe_sharpe(series: pd.Series) -> float:
    std = float(series.std())
    if std == 0.0 or np.isnan(std):
        return 0.0
    return float(series.mean() / std)
