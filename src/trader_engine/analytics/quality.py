from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np
import pandas as pd


@dataclass
class EdgeQualityResult:
    summary: pd.DataFrame
    fold_consistency: pd.DataFrame


def edge_quality(
    trades: pd.DataFrame,
    group_by: str | Sequence[str],
    min_group_observations: int = 1,
    fold_col: str = "fold_id",
) -> EdgeQualityResult:
    if trades.empty:
        empty = pd.DataFrame()
        return EdgeQualityResult(summary=empty, fold_consistency=empty)

    groups = [group_by] if isinstance(group_by, str) else list(group_by)
    ordered = _ordered_trades(trades)
    overall_drawdown = _max_drawdown(ordered["return_pct"])

    summary = (
        ordered.groupby(groups)
        .agg(
            sample_size=("symbol", "count"),
            mean_return=("return_pct", "mean"),
            out_of_sample_sharpe=("return_pct", _safe_sharpe),
            win_rate=("return_pct", lambda series: (series > 0).mean()),
            average_win=("return_pct", lambda series: series[series > 0].mean() if (series > 0).any() else 0.0),
            average_loss=("return_pct", lambda series: series[series < 0].mean() if (series < 0).any() else 0.0),
            expectancy=("return_pct", "mean"),
        )
        .reset_index()
    )
    if summary.empty:
        return EdgeQualityResult(summary=summary, fold_consistency=pd.DataFrame())

    drawdown_by_group = (
        ordered.groupby(groups)
        .apply(lambda frame: _max_drawdown(frame["return_pct"]))
        .rename("max_drawdown_group")
        .reset_index()
    )
    summary = summary.merge(drawdown_by_group, on=groups, how="left")
    summary["max_drawdown_contribution"] = (
        summary["max_drawdown_group"] / overall_drawdown if overall_drawdown > 0 else 0.0
    )

    if fold_col in ordered.columns:
        fold_detail = (
            ordered.groupby([*groups, fold_col])
            .agg(
                fold_sample_size=("symbol", "count"),
                fold_expectancy=("return_pct", "mean"),
                fold_sharpe=("return_pct", _safe_sharpe),
            )
            .reset_index()
        )
        fold_consistency = (
            fold_detail.groupby(groups)
            .agg(
                active_fold_count=(fold_col, "nunique"),
                positive_fold_fraction=("fold_expectancy", lambda series: (series > 0).mean()),
                expectancy_variance=("fold_expectancy", "var"),
                sharpe_variance=("fold_sharpe", "var"),
                mean_fold_expectancy=("fold_expectancy", "mean"),
                mean_fold_sharpe=("fold_sharpe", "mean"),
            )
            .reset_index()
        )
        fold_consistency["expectancy_variance"] = fold_consistency["expectancy_variance"].fillna(0.0)
        fold_consistency["sharpe_variance"] = fold_consistency["sharpe_variance"].fillna(0.0)
    else:
        fold_consistency = pd.DataFrame(columns=[*groups, "active_fold_count", "positive_fold_fraction", "expectancy_variance", "sharpe_variance"])

    summary = summary.loc[summary["sample_size"] >= min_group_observations].copy()
    if summary.empty:
        return EdgeQualityResult(summary=summary, fold_consistency=fold_consistency)

    summary = summary.merge(fold_consistency, on=groups, how="left")
    summary["active_fold_count"] = summary["active_fold_count"].fillna(0).astype(int)
    summary["positive_fold_fraction"] = summary["positive_fold_fraction"].fillna(0.0)
    summary["expectancy_variance"] = summary["expectancy_variance"].fillna(0.0)
    summary["sharpe_variance"] = summary["sharpe_variance"].fillna(0.0)
    summary["consistency_score"] = _blend_scores(
        {
            "active_fold_count": summary["active_fold_count"],
            "positive_fold_fraction": summary["positive_fold_fraction"],
            "expectancy_variance": -summary["expectancy_variance"],
            "sharpe_variance": -summary["sharpe_variance"],
        }
    )
    summary["quality_score"] = _blend_scores(
        {
            "expectancy": summary["expectancy"],
            "out_of_sample_sharpe": summary["out_of_sample_sharpe"],
            "win_rate": summary["win_rate"],
            "sample_size": np.log1p(summary["sample_size"]),
            "consistency_score": summary["consistency_score"],
            "drawdown": -summary["max_drawdown_contribution"],
        },
        weights={
            "expectancy": 0.25,
            "out_of_sample_sharpe": 0.25,
            "win_rate": 0.10,
            "sample_size": 0.15,
            "consistency_score": 0.20,
            "drawdown": 0.05,
        },
    )
    summary["sample_size_score"] = _normalize(np.log1p(summary["sample_size"].astype(float)))
    ranking_groups = groups[:-1]
    summary = _add_rank_metrics(summary, score_column="quality_score", prefix="quality", group_cols=ranking_groups)
    summary = _add_rank_metrics(
        summary,
        score_column="consistency_score",
        prefix="consistency",
        group_cols=ranking_groups,
    )
    summary = summary.sort_values(["quality_score", "expectancy", "sample_size"], ascending=[False, False, False]).reset_index(drop=True)
    return EdgeQualityResult(summary=summary, fold_consistency=fold_consistency)


def family_performance(trades: pd.DataFrame, min_group_observations: int = 1) -> pd.DataFrame:
    if trades.empty or "entry_state_family" not in trades.columns:
        return pd.DataFrame()
    groups: list[str] = []
    if "asset_class" in trades.columns:
        groups.append("asset_class")
    groups.append("entry_state_family")
    return edge_quality(
        trades=trades,
        group_by=groups,
        min_group_observations=min_group_observations,
    ).summary


def _ordered_trades(trades: pd.DataFrame) -> pd.DataFrame:
    order_column = "exit_timestamp" if "exit_timestamp" in trades.columns else "entry_timestamp"
    if order_column in trades.columns:
        return trades.sort_values(order_column).copy()
    return trades.copy()


def _safe_sharpe(series: pd.Series) -> float:
    std = float(series.std())
    if std == 0.0 or np.isnan(std):
        return 0.0
    return float(series.mean() / std)


def _max_drawdown(series: pd.Series) -> float:
    if series.empty:
        return 0.0
    cumulative = (1.0 + series.fillna(0.0)).cumprod()
    running_peak = cumulative.cummax().clip(lower=1.0)
    drawdown = cumulative / running_peak - 1.0
    return float(abs(drawdown.min())) if not drawdown.empty else 0.0


def _blend_scores(components: dict[str, pd.Series], weights: dict[str, float] | None = None) -> pd.Series:
    if not components:
        return pd.Series(dtype=float)
    weighted = None
    weight_sum = 0.0
    for name, series in components.items():
        normalized = _normalize(series.astype(float).fillna(0.0))
        weight = float(weights[name]) if weights and name in weights else 1.0
        weight_sum += weight
        contribution = normalized * weight
        weighted = contribution if weighted is None else weighted + contribution
    if weighted is None or weight_sum == 0.0:
        return pd.Series(0.0, index=next(iter(components.values())).index)
    return weighted / weight_sum


def _normalize(series: pd.Series) -> pd.Series:
    minimum = float(series.min())
    maximum = float(series.max())
    if maximum == minimum:
        return pd.Series(0.5, index=series.index)
    return (series - minimum) / (maximum - minimum)


def _add_rank_metrics(
    frame: pd.DataFrame,
    score_column: str,
    prefix: str,
    group_cols: list[str],
) -> pd.DataFrame:
    ranked = frame.copy()
    if group_cols:
        rank_series = ranked.groupby(group_cols)[score_column].rank(method="dense", ascending=False)
        group_size = ranked.groupby(group_cols)[score_column].transform("count").astype(float)
    else:
        rank_series = ranked[score_column].rank(method="dense", ascending=False)
        group_size = pd.Series(float(len(ranked)), index=ranked.index)
    ranked[f"{prefix}_rank"] = rank_series.astype(int)
    ranked[f"{prefix}_group_size"] = group_size.astype(int)
    ranked[f"{prefix}_percentile"] = 1.0 - ((rank_series - 1.0) / group_size.clip(lower=1.0))
    ranked[f"{prefix}_percentile"] = ranked[f"{prefix}_percentile"].clip(lower=0.0, upper=1.0)
    return ranked
