from __future__ import annotations

import pandas as pd

from trader_engine.core.config import MarkovConfig
from trader_engine.markov.engine import MarkovAnalyzer


def test_markov_analyzer_builds_transition_probabilities() -> None:
    index = pd.date_range("2023-01-01", periods=12, freq="D")
    frame = pd.DataFrame(
        {
            "close": [100, 101, 102, 101, 100, 99, 98, 99, 100, 101, 102, 103],
            "state": [
                "up|normal|strong|normal",
                "up|normal|strong|normal",
                "up|normal|strong|normal",
                "down|high|weak|normal",
                "down|high|weak|normal",
                "down|high|weak|normal",
                "down|high|weak|oversold",
                "up|low|strong|normal",
                "up|low|strong|normal",
                "up|normal|strong|normal",
                "up|normal|strong|normal",
                "up|normal|strong|normal",
            ],
        },
        index=index,
    )

    analyzer = MarkovAnalyzer(MarkovConfig(transition_horizon_bars=1, forward_return_horizon_bars=2))
    analysis = analyzer.analyze(frame)

    assert not analysis.transition_matrix.empty
    assert all(abs(row_sum - 1.0) < 1e-9 for row_sum in analysis.transition_matrix.sum(axis=1))
    assert "mean_return" in analysis.state_returns.columns
    assert analysis.accuracy >= 0.0
