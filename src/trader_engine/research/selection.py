from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

GATE_CHECK_ORDER = [
    "family_allowed_pass",
    "family_excluded_pass",
    "family_top_count_pass",
    "family_bottom_exclusion_pass",
    "state_quality_score_pass",
    "state_quality_percentile_pass",
    "state_quality_rank_pass",
    "state_trade_count_pass",
    "state_active_folds_pass",
    "state_positive_fold_fraction_pass",
    "state_expectancy_variance_pass",
    "state_sharpe_variance_pass",
    "transition_quality_score_pass",
    "transition_quality_percentile_pass",
    "transition_quality_rank_pass",
    "transition_trade_count_pass",
    "transition_active_folds_pass",
    "transition_positive_fold_fraction_pass",
    "transition_expectancy_variance_pass",
    "transition_sharpe_variance_pass",
    "consistency_score_pass",
    "consistency_percentile_pass",
    "consistency_rank_pass",
]


@dataclass
class OpportunityCapResult:
    frames_by_symbol: dict[str, pd.DataFrame]
    candidates: pd.DataFrame


def apply_opportunity_caps(frames_by_symbol: dict[str, pd.DataFrame]) -> OpportunityCapResult:
    updated_frames = {symbol: frame.copy() for symbol, frame in frames_by_symbol.items()}
    combined = _combine_candidate_frames(updated_frames)
    if combined.empty:
        return OpportunityCapResult(frames_by_symbol=updated_frames, candidates=combined)

    combined["rebalance_cap_pass"] = True
    combined["asset_class_cap_pass"] = True
    combined["model_cap_pass"] = True
    combined["cap_passed"] = True
    combined["selection_rank"] = pd.NA
    combined["asset_class_rank"] = pd.NA
    combined["model_rank"] = pd.NA
    combined["final_selection_passed"] = combined.get("selection_passed", False)

    active_candidates = combined["selection_passed"].astype(bool) & combined["signal"].isin(["long", "short"])
    if active_candidates.any():
        ranked = combined.loc[active_candidates].copy()
        ranked["selection_rank"] = ranked.groupby("timestamp")["signal_score"].rank(
            method="first",
            ascending=False,
        )
        ranked["asset_class_rank"] = ranked.groupby(["timestamp", "asset_class"])["signal_score"].rank(
            method="first",
            ascending=False,
        )
        if "model_name" in ranked.columns:
            ranked["model_rank"] = ranked.groupby(["timestamp", "model_name"])["signal_score"].rank(
                method="first",
                ascending=False,
            )
        else:
            ranked["model_rank"] = ranked["selection_rank"]
        ranked["rebalance_cap_pass"] = ranked.apply(_rebalance_cap_pass, axis=1)
        ranked["asset_class_cap_pass"] = ranked.apply(_asset_class_cap_pass, axis=1)
        ranked["model_cap_pass"] = ranked.apply(_model_cap_pass, axis=1)
        ranked["cap_passed"] = (
            ranked["rebalance_cap_pass"] & ranked["asset_class_cap_pass"] & ranked["model_cap_pass"]
        )
        ranked["final_selection_passed"] = ranked["selection_passed"] & ranked["cap_passed"]
        combined.loc[ranked.index, ranked.columns] = ranked

    for symbol, frame in updated_frames.items():
        symbol_rows = combined.loc[combined["symbol"] == symbol].copy()
        if symbol_rows.empty:
            continue
        symbol_rows = symbol_rows.set_index("timestamp")
        for column in [
            "rebalance_cap_pass",
            "asset_class_cap_pass",
            "model_cap_pass",
            "cap_passed",
            "selection_rank",
            "asset_class_rank",
            "model_rank",
            "final_selection_passed",
        ]:
            frame.loc[symbol_rows.index, column] = symbol_rows[column]
        fail_mask = frame["selection_passed"].astype(bool) & ~frame["cap_passed"].fillna(True).astype(bool)
        if fail_mask.any():
            frame.loc[fail_mask, "signal"] = "flat"
        updated_frames[symbol] = frame

    return OpportunityCapResult(frames_by_symbol=updated_frames, candidates=combined)


def selection_report_frames(candidates: pd.DataFrame) -> dict[str, pd.DataFrame]:
    if candidates.empty:
        return {}

    frames = {
        "signal_candidates": candidates,
        "gate_rejection_summary": _gate_breakdown(candidates, []),
        "gate_rejection_by_asset_class": _gate_breakdown(candidates, _available_group_cols(candidates, ["asset_class"])),
        "gate_rejection_by_fold": _gate_breakdown(candidates, _available_group_cols(candidates, ["split", "fold_id", "asset_class"])),
        "gate_starvation_summary": _starvation_summary(candidates),
        "quality_score_distributions": _score_distributions(candidates),
        "score_component_summary": _score_component_summary(candidates),
        "score_component_distributions": _score_component_distributions(candidates),
        "hard_filter_summary": _hard_filter_summary(candidates),
        "family_filter_summary": _family_filter_summary(candidates),
        "top_n_selection_summary": _top_n_summary(candidates),
    }
    return frames


def annotate_opportunity_ranks(opportunities: pd.DataFrame) -> pd.DataFrame:
    if opportunities.empty:
        return opportunities
    ranked = opportunities.copy()
    ranked["selection_rank"] = ranked["score"].rank(method="first", ascending=False)
    if "asset_class" in ranked.columns:
        ranked["asset_class_rank"] = ranked.groupby("asset_class")["score"].rank(method="first", ascending=False)
    else:
        ranked["asset_class_rank"] = ranked["selection_rank"]
    if "model_name" in ranked.columns:
        ranked["model_rank"] = ranked.groupby("model_name")["score"].rank(method="first", ascending=False)
    else:
        ranked["model_rank"] = ranked["selection_rank"]
    ranked["rebalance_cap_pass"] = ranked.apply(_rebalance_cap_pass, axis=1)
    ranked["asset_class_cap_pass"] = ranked.apply(_asset_class_cap_pass, axis=1)
    ranked["model_cap_pass"] = ranked.apply(_model_cap_pass, axis=1)
    ranked["cap_passed"] = (
        ranked["rebalance_cap_pass"] & ranked["asset_class_cap_pass"] & ranked["model_cap_pass"]
    )
    ranked["final_selection_passed"] = ranked.get("selection_passed", False).fillna(False).astype(bool) & ranked[
        "cap_passed"
    ].fillna(True).astype(bool)
    return ranked


def _combine_candidate_frames(frames_by_symbol: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    for symbol, frame in frames_by_symbol.items():
        if frame.empty:
            continue
        enriched = frame.copy()
        enriched["timestamp"] = enriched.index
        enriched["symbol"] = symbol
        rows.append(enriched.reset_index(drop=True))
    if not rows:
        return pd.DataFrame()
    combined = pd.concat(rows, ignore_index=True)
    return combined


def _rebalance_cap_pass(row: pd.Series) -> bool:
    limits = [
        _safe_limit(row.get("selection_cap_overall_limit")),
        _safe_limit(row.get("selection_cap_model_limit")),
    ]
    active = [limit for limit in limits if limit is not None]
    if not active:
        return True
    return float(row["selection_rank"]) <= float(min(active))


def _asset_class_cap_pass(row: pd.Series) -> bool:
    limit = _safe_limit(row.get("selection_cap_asset_class_limit"))
    if limit is None:
        return True
    return float(row["asset_class_rank"]) <= float(limit)


def _model_cap_pass(row: pd.Series) -> bool:
    limit = _safe_limit(row.get("selection_cap_model_limit"))
    if limit is None:
        return True
    return float(row["model_rank"]) <= float(limit)


def _safe_limit(value: object) -> int | None:
    if value in {None, "", 0}:
        return None
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _candidate_universe(candidates: pd.DataFrame) -> pd.DataFrame:
    if candidates.empty:
        return candidates
    if "candidate_signal" not in candidates.columns:
        return pd.DataFrame()
    return candidates.loc[candidates["candidate_signal"].isin(["long", "short"])].copy()


def _gate_breakdown(candidates: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    universe = _candidate_universe(candidates)
    if universe.empty:
        return pd.DataFrame()

    available_checks = [column for column in GATE_CHECK_ORDER if column in universe.columns]
    rows: list[dict[str, object]] = []
    grouped = [((), universe)] if not group_cols else universe.groupby(group_cols, dropna=False)
    for group_key, frame in grouped:
        total_candidates = len(frame)
        if total_candidates == 0:
            continue
        remaining = pd.Series(True, index=frame.index)
        if not isinstance(group_key, tuple):
            group_key = (group_key,)
        context = {column: value for column, value in zip(group_cols, group_key)}
        for gate_name in available_checks:
            gate_pass = frame[gate_name].fillna(False).astype(bool)
            rejected = remaining & ~gate_pass
            rejected_count = int(rejected.sum())
            remaining = remaining & gate_pass
            rows.append(
                {
                    **context,
                    "gate_name": gate_name,
                    "candidate_count": total_candidates,
                    "rejected_count": rejected_count,
                    "rejection_rate": rejected_count / max(total_candidates, 1),
                    "incremental_rejection_rate": rejected_count / max(int((remaining | rejected).sum()), 1),
                    "cumulative_rejection_rate": 1.0 - (int(remaining.sum()) / max(total_candidates, 1)),
                    "remaining_count": int(remaining.sum()),
                    "final_selected_count": int(frame["final_selection_passed"].fillna(False).astype(bool).sum()),
                }
            )
    return pd.DataFrame(rows)


def _starvation_summary(candidates: pd.DataFrame) -> pd.DataFrame:
    group_cols = _available_group_cols(candidates, ["split", "fold_id", "asset_class"])
    summary = _gate_breakdown(candidates, group_cols)
    if summary.empty:
        return pd.DataFrame()
    sort_cols = [*group_cols, "rejected_count"]
    ascending = [True] * len(group_cols) + [False]
    ordered = summary.sort_values(sort_cols, ascending=ascending)
    return ordered.groupby(group_cols, as_index=False).first() if group_cols else ordered.head(1)


def _score_distributions(candidates: pd.DataFrame) -> pd.DataFrame:
    universe = _candidate_universe(candidates)
    if universe.empty:
        return pd.DataFrame()
    metrics = [
        column
        for column in [
            "state_quality_score",
            "transition_quality_score",
            "fold_consistency_score",
            "sample_size_score",
            "tradability_score",
            "signal_score",
        ]
        if column in universe.columns
    ]
    rows: list[dict[str, object]] = []
    for metric in metrics:
        group_cols = _available_group_cols(universe, ["split", "asset_class"])
        working = universe[[metric, *group_cols]].dropna().copy()
        if working.empty:
            continue
        working["score_bin"] = pd.cut(
            working[metric].astype(float),
            bins=[-0.001, 0.2, 0.4, 0.6, 0.8, 1.0, 10.0],
            labels=["0.0-0.2", "0.2-0.4", "0.4-0.6", "0.6-0.8", "0.8-1.0", ">1.0"],
        )
        grouped = (
            working.groupby([*group_cols, "score_bin"], observed=False)
            .size()
            .reset_index(name="count")
        )
        grouped["metric"] = metric
        rows.append(grouped)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def _score_component_summary(candidates: pd.DataFrame) -> pd.DataFrame:
    universe = _candidate_universe(candidates)
    if universe.empty:
        return pd.DataFrame()
    component_cols = [
        column
        for column in universe.columns
        if column.startswith("score_component_") or column.startswith("score_contribution_")
    ]
    if not component_cols:
        return pd.DataFrame()
    working = universe.copy()
    working["selection_status"] = np.where(
        working["final_selection_passed"].fillna(False).astype(bool),
        "selected",
        "rejected",
    )
    melted = working.melt(
        id_vars=_available_group_cols(working, ["split", "asset_class", "selection_status"]),
        value_vars=component_cols,
        var_name="component",
        value_name="value",
    ).dropna(subset=["value"])
    if melted.empty:
        return pd.DataFrame()
    group_cols = _available_group_cols(melted, ["split", "asset_class", "selection_status"]) + ["component"]
    return (
        melted.groupby(group_cols, dropna=False)
        .agg(
            mean_value=("value", "mean"),
            median_value=("value", "median"),
            max_value=("value", "max"),
            min_value=("value", "min"),
            count=("value", "count"),
        )
        .reset_index()
    )


def _score_component_distributions(candidates: pd.DataFrame) -> pd.DataFrame:
    universe = _candidate_universe(candidates)
    if universe.empty:
        return pd.DataFrame()
    metric_cols = [
        column
        for column in universe.columns
        if column.startswith("score_component_") or column.startswith("score_contribution_")
    ]
    rows: list[pd.DataFrame] = []
    for metric in metric_cols:
        group_cols = _available_group_cols(universe, ["split", "asset_class"])
        working = universe[[metric, *group_cols]].dropna().copy()
        if working.empty:
            continue
        working["score_bin"] = pd.cut(
            working[metric].astype(float),
            bins=[-0.001, 0.05, 0.15, 0.30, 0.50, 1.0, 10.0],
            labels=["0.00-0.05", "0.05-0.15", "0.15-0.30", "0.30-0.50", "0.50-1.00", ">1.00"],
        )
        grouped = (
            working.groupby([*group_cols, "score_bin"], observed=False)
            .size()
            .reset_index(name="count")
        )
        grouped["metric"] = metric
        rows.append(grouped)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def _hard_filter_summary(candidates: pd.DataFrame) -> pd.DataFrame:
    universe = _candidate_universe(candidates)
    if universe.empty or "hard_block_reason" not in universe.columns:
        return pd.DataFrame()
    blocked = universe.loc[~universe["selection_passed"].fillna(False).astype(bool)].copy()
    if blocked.empty:
        return pd.DataFrame()
    group_cols = _available_group_cols(blocked, ["split", "fold_id", "asset_class"])
    grouped = (
        blocked.groupby([*group_cols, "hard_block_category", "hard_block_reason"], dropna=False)
        .size()
        .reset_index(name="blocked_count")
    )
    totals = (
        universe.groupby(group_cols, dropna=False).size().reset_index(name="candidate_count")
        if group_cols
        else pd.DataFrame([{"candidate_count": len(universe)}])
    )
    if group_cols:
        grouped = grouped.merge(totals, on=group_cols, how="left")
    else:
        grouped["candidate_count"] = len(universe)
    grouped["blocked_rate"] = grouped["blocked_count"] / grouped["candidate_count"].clip(lower=1)
    return grouped.sort_values("blocked_count", ascending=False).reset_index(drop=True)


def _family_filter_summary(candidates: pd.DataFrame) -> pd.DataFrame:
    universe = _candidate_universe(candidates)
    if universe.empty or "state_family" not in universe.columns:
        return pd.DataFrame()
    group_cols = _available_group_cols(universe, ["split", "asset_class"]) + ["state_family"]
    summary = (
        universe.groupby(group_cols, dropna=False)
        .agg(
            candidate_count=("state_family", "count"),
            family_gate_pass_rate=("family_gate_pass", lambda series: series.fillna(False).astype(bool).mean()),
            hard_filter_pass_rate=("hard_filter_passed", lambda series: series.fillna(False).astype(bool).mean()),
            selected_rate=("final_selection_passed", lambda series: series.fillna(False).astype(bool).mean()),
            mean_family_quality_score=("state_family_quality_score", "mean"),
            mean_family_rank_score=("family_rank_score", "mean"),
            mean_family_consistency_score=("state_family_consistency_score", "mean"),
        )
        .reset_index()
    )
    return summary.sort_values(["selected_rate", "candidate_count"], ascending=[False, False]).reset_index(drop=True)


def _top_n_summary(candidates: pd.DataFrame) -> pd.DataFrame:
    if candidates.empty:
        return pd.DataFrame()
    selected = candidates.loc[candidates["selection_passed"].fillna(False).astype(bool)].copy()
    if selected.empty:
        return pd.DataFrame()
    group_cols = _available_group_cols(selected, ["split", "fold_id", "asset_class", "model_name"])
    if not group_cols:
        group_cols = ["asset_class"] if "asset_class" in selected.columns else []
    if not group_cols:
        selected = selected.assign(scope="all")
        group_cols = ["scope"]
    return (
        selected.groupby(group_cols, dropna=False)
        .agg(
            pre_cap_count=("selection_passed", "sum"),
            post_cap_count=("final_selection_passed", "sum"),
            capped_out_count=("cap_passed", lambda series: (~series.fillna(True).astype(bool)).sum()),
            median_selection_rank=("selection_rank", "median"),
            median_asset_class_rank=("asset_class_rank", "median"),
        )
        .reset_index()
    )


def _available_group_cols(frame: pd.DataFrame, columns: list[str]) -> list[str]:
    return [column for column in columns if column in frame.columns]
