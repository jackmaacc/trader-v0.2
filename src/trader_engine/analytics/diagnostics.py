from __future__ import annotations

from math import sqrt
from typing import Sequence

import numpy as np
import pandas as pd


def summarize_trade_groups(
    trades: pd.DataFrame,
    group_by: str | Sequence[str],
    min_group_observations: int = 1,
) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()

    groups = [group_by] if isinstance(group_by, str) else list(group_by)
    records: list[dict[str, object]] = []
    for keys, group in trades.groupby(groups, dropna=False):
        if len(group) < min_group_observations:
            continue
        record = _normalize_group_keys(groups, keys)
        record.update(_trade_summary_record(group))
        record["edge_classification"] = _classify_edge(record)
        records.append(record)

    if not records:
        return pd.DataFrame()
    summary = pd.DataFrame(records)
    return summary.sort_values(["expectancy", "trade_count"], ascending=[False, False]).reset_index(drop=True)


def performance_by_volatility_regime(trades: pd.DataFrame, min_group_observations: int = 1) -> pd.DataFrame:
    return _safe_group_summary(trades, "entry_volatility_regime", min_group_observations)


def performance_by_asset_class(trades: pd.DataFrame, min_group_observations: int = 1) -> pd.DataFrame:
    return _safe_group_summary(trades, "asset_class", min_group_observations)


def performance_by_exit_reason(trades: pd.DataFrame, min_group_observations: int = 1) -> pd.DataFrame:
    return _safe_group_summary(trades, "exit_reason", min_group_observations)


def performance_by_state_family(trades: pd.DataFrame, min_group_observations: int = 1) -> pd.DataFrame:
    return _safe_group_summary(trades, "entry_state_family", min_group_observations)


def performance_by_state_model(trades: pd.DataFrame, min_group_observations: int = 1) -> pd.DataFrame:
    return _safe_group_summary(trades, "model_name", min_group_observations)


def performance_by_holding_period_bucket(trades: pd.DataFrame, min_group_observations: int = 1) -> pd.DataFrame:
    return _safe_group_summary(trades, "holding_period_bucket", min_group_observations)


def state_edge_summary(trades: pd.DataFrame, min_group_observations: int = 1) -> pd.DataFrame:
    return _safe_group_summary(trades, "entry_state", min_group_observations)


def transition_edge_summary(trades: pd.DataFrame, min_group_observations: int = 1) -> pd.DataFrame:
    return _safe_group_summary(trades, "transition_type", min_group_observations)


def payoff_asymmetry_summary(
    trades: pd.DataFrame,
    group_by: str | Sequence[str] | None = None,
    min_group_observations: int = 1,
) -> pd.DataFrame:
    if group_by is None:
        if trades.empty:
            return pd.DataFrame()
        return pd.DataFrame([_trade_summary_record(trades)])
    return summarize_trade_groups(trades, group_by, min_group_observations=min_group_observations)


def excursion_distribution(
    trades: pd.DataFrame,
    column: str,
    value_mode: str = "raw",
    bins: Sequence[float] | None = None,
) -> pd.DataFrame:
    if trades.empty or column not in trades.columns:
        return pd.DataFrame()

    series = pd.to_numeric(trades[column], errors="coerce")
    if value_mode == "adverse_positive":
        values = (-series).clip(lower=0.0)
    else:
        values = series.clip(lower=0.0)

    bucket_edges = list(bins or [0.0, 0.005, 0.01, 0.02, 0.03, np.inf])
    bucket_labels = _bucket_labels(bucket_edges)
    bucketed = pd.cut(values, bins=bucket_edges, labels=bucket_labels, include_lowest=True, right=False)
    frame = pd.DataFrame(
        {
            "bucket": bucketed.astype("string"),
            "value": values,
            "return_pct": pd.to_numeric(trades.get("return_pct"), errors="coerce"),
        }
    ).dropna(subset=["bucket"])
    if frame.empty:
        return pd.DataFrame()
    total = len(frame)
    summary = (
        frame.groupby("bucket", observed=False)
        .agg(
            trade_count=("value", "count"),
            average_value=("value", "mean"),
            median_value=("value", "median"),
            average_return=("return_pct", "mean"),
        )
        .reset_index()
    )
    summary["share_of_trades"] = summary["trade_count"] / total
    return summary


def trade_diagnostics(trades: pd.DataFrame, top_n: int = 25) -> dict[str, pd.DataFrame]:
    if trades.empty:
        empty = pd.DataFrame()
        return {
            "best_trades": empty,
            "worst_trades": empty,
            "failure_modes": empty,
            "payoff_asymmetry_summary": empty,
            "asymmetry_by_exit_reason": empty,
            "asymmetry_by_asset_class": empty,
            "asymmetry_by_state_model": empty,
            "asymmetry_by_state_family": empty,
            "asymmetry_by_holding_period_bucket": empty,
            "adverse_excursion_distribution": empty,
            "uncaptured_favorable_distribution": empty,
        }

    ordered = trades.sort_values("return_pct", ascending=False).reset_index(drop=True)
    best = ordered.head(top_n).copy()
    worst = ordered.tail(top_n).sort_values("return_pct", ascending=True).reset_index(drop=True)
    failure_modes = build_failure_modes(trades, min_group_observations=max(3, min(top_n, 10)))
    return {
        "best_trades": best,
        "worst_trades": worst,
        "failure_modes": failure_modes,
        "payoff_asymmetry_summary": payoff_asymmetry_summary(trades),
        "asymmetry_by_exit_reason": performance_by_exit_reason(trades),
        "asymmetry_by_asset_class": performance_by_asset_class(trades),
        "asymmetry_by_state_model": performance_by_state_model(trades),
        "asymmetry_by_state_family": performance_by_state_family(trades),
        "asymmetry_by_holding_period_bucket": performance_by_holding_period_bucket(trades),
        "adverse_excursion_distribution": excursion_distribution(trades, "mae_pct", value_mode="adverse_positive"),
        "uncaptured_favorable_distribution": excursion_distribution(
            trades,
            "favorable_excursion_left_uncaptured_pct",
            value_mode="positive",
        ),
    }


def build_failure_modes(trades: pd.DataFrame, min_group_observations: int = 3) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()

    summary = (
        trades.groupby(["asset_class", "exit_reason", "entry_state"], dropna=False)
        .apply(lambda frame: pd.Series(_trade_summary_record(frame)))
        .reset_index()
    )
    summary = summary.loc[summary["trade_count"] >= min_group_observations].copy()
    if summary.empty:
        return summary
    summary["failure_mode"] = (
        summary["asset_class"].astype(str)
        + " | "
        + summary["exit_reason"].astype(str)
        + " | "
        + summary["entry_state"].astype(str)
    )
    return summary.sort_values(["trade_count", "expectancy"], ascending=[False, True]).reset_index(drop=True)


def _trade_summary_record(trades: pd.DataFrame) -> dict[str, float]:
    returns = pd.to_numeric(trades.get("return_pct"), errors="coerce").fillna(0.0)
    pnl = pd.to_numeric(trades.get("pnl"), errors="coerce").fillna(0.0)
    wins = returns.loc[returns > 0]
    losses = returns.loc[returns <= 0]
    adverse_excursion = (-pd.to_numeric(trades.get("mae_pct"), errors="coerce")).clip(lower=0.0)
    favorable_excursion = pd.to_numeric(trades.get("mfe_pct"), errors="coerce").clip(lower=0.0)
    uncaptured_favorable = pd.to_numeric(
        trades.get("favorable_excursion_left_uncaptured_pct"),
        errors="coerce",
    ).clip(lower=0.0)

    average_loss = float(losses.mean()) if not losses.empty else 0.0
    average_win = float(wins.mean()) if not wins.empty else 0.0
    loss_magnitude = abs(average_loss)
    return {
        "trade_count": int(len(trades)),
        "win_rate": float((returns > 0).mean()),
        "expectancy": float(returns.mean()),
        "average_return": float(returns.mean()),
        "average_pnl": float(pnl.mean()),
        "average_win": average_win,
        "average_loss": average_loss,
        "win_loss_size_ratio": average_win / loss_magnitude if loss_magnitude > 0 else np.nan,
        "sharpe": _trade_sharpe(returns),
        "average_expected_value": float(pd.to_numeric(trades.get("entry_expected_value"), errors="coerce").mean()),
        "average_expected_return": float(pd.to_numeric(trades.get("entry_expected_return"), errors="coerce").mean()),
        "average_gap": float(pd.to_numeric(trades.get("expected_vs_realized_gap"), errors="coerce").mean()),
        "average_hold_bars": float(pd.to_numeric(trades.get("hold_bars"), errors="coerce").mean()),
        "average_mae_pct": float(pd.to_numeric(trades.get("mae_pct"), errors="coerce").mean()),
        "average_mfe_pct": float(pd.to_numeric(trades.get("mfe_pct"), errors="coerce").mean()),
        "average_adverse_excursion_pct": float(adverse_excursion.mean()),
        "median_adverse_excursion_pct": float(adverse_excursion.median()),
        "average_favorable_excursion_pct": float(favorable_excursion.mean()),
        "average_favorable_excursion_left_uncaptured_pct": float(uncaptured_favorable.mean()),
        "median_favorable_excursion_left_uncaptured_pct": float(uncaptured_favorable.median()),
    }


def _trade_sharpe(returns: pd.Series) -> float:
    clean = pd.to_numeric(returns, errors="coerce").dropna()
    if clean.empty:
        return 0.0
    std = float(clean.std(ddof=0))
    if std == 0.0:
        return 0.0
    return float(clean.mean() / std * sqrt(len(clean)))


def _normalize_group_keys(groups: list[str], keys: object) -> dict[str, object]:
    values = keys if isinstance(keys, tuple) else (keys,)
    return {group: value for group, value in zip(groups, values, strict=False)}


def _bucket_labels(edges: Sequence[float]) -> list[str]:
    labels: list[str] = []
    for start, end in zip(edges[:-1], edges[1:], strict=False):
        if np.isinf(end):
            labels.append(f"{start:.1%}+")
        else:
            labels.append(f"{start:.1%}-{end:.1%}")
    return labels


def _classify_edge(row: pd.Series | dict[str, object]) -> str:
    expectancy = float(row.get("expectancy", 0.0))
    expected_value = float(row.get("average_expected_value", 0.0))
    if expectancy > 0 and expected_value > 0:
        return "positive"
    if expectancy < 0 and expected_value <= 0:
        return "weak"
    return "mixed"


def _safe_group_summary(
    trades: pd.DataFrame,
    group_by: str | Sequence[str],
    min_group_observations: int,
) -> pd.DataFrame:
    groups = [group_by] if isinstance(group_by, str) else list(group_by)
    if trades.empty or any(group not in trades.columns for group in groups):
        return pd.DataFrame()
    return summarize_trade_groups(trades, groups, min_group_observations=min_group_observations)
