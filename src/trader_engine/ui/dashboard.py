from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from trader_engine.ui.market_catalog import render_market_catalog
from trader_engine.ui.net_edge import render_net_edge
from trader_engine.ui.crypto_service import render_crypto_service

CORE_STATE_ARTIFACTS = {
    "state_frequency",
    "state_persistence",
    "state_quality",
    "transition_quality",
    "state_tradability",
    "sparse_states",
    "summary",
}


def render_dashboard(artifacts_dir: Path) -> None:
    st.set_page_config(page_title="trader-v0.2 Research Engine", layout="wide")
    _inject_styles()

    st.markdown(
        """
        <div class="hero">
          <div class="eyebrow">trader-v0.2</div>
          <h1>Quantitative Research & Trading Engine</h1>
          <p>Probabilistic market-state modeling, Markov transitions, robustness diagnostics, and out-of-sample sensitivity analysis.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.info("Research results only. Historical rankings and simulated returns do not establish readiness to trade. No strategy has been approved by this dashboard.")
    render_crypto_service(artifacts_dir)
    render_market_catalog(artifacts_dir.parent / 'market_catalog' / 'catalog.json')

    has_net_edge = render_net_edge(artifacts_dir)
    base = _load_base_artifacts(artifacts_dir)
    research = _load_research_artifacts(artifacts_dir)

    if (
        base["opportunities"].empty
        and research["walk_fold_metrics"].empty
        and research["sweep_summary"].empty
        and research["comparison_summary"].empty
    ):
        if not has_net_edge:
            st.warning(f"No research artifacts found in {artifacts_dir}. Run the research workflows first.")
        return

    tabs = st.tabs(
        [
            "Overview",
            "Symbol Detail",
            "State Diagnostics",
            "Walk-Forward",
            "Edge Quality",
            "Gate Ablation",
            "Policy Comparison",
            "Exit Research",
            "Parameter Sweeps",
            "State Model Comparison",
            "State Attribution",
            "Failure Analysis",
        ]
    )

    with tabs[0]:
        _render_overview(base, research)
    with tabs[1]:
        _render_symbol_detail(base, artifacts_dir)
    with tabs[2]:
        _render_state_diagnostics(base)
    with tabs[3]:
        _render_walk_forward(research)
    with tabs[4]:
        _render_edge_quality(base, research)
    with tabs[5]:
        _render_gate_ablation(research)
    with tabs[6]:
        _render_selection_policy_comparison(research)
    with tabs[7]:
        _render_exit_research(research)
    with tabs[8]:
        _render_parameter_sweeps(research)
    with tabs[9]:
        _render_state_model_comparison(research)
    with tabs[10]:
        _render_state_attribution(base, research)
    with tabs[11]:
        _render_failure_analysis(base, research)


def _render_overview(base: dict[str, object], research: dict[str, object]) -> None:
    opportunities = base["opportunities"]
    metrics = base["metrics"]
    equity_curve = base["equity_curve"]
    current_state_distribution = base["current_state_distribution"]
    comparison_summary = research["comparison_summary"]

    metric_columns = st.columns(6)
    metric_columns[0].metric("Total Return", _fmt_pct(metrics.get("total_return", 0.0)))
    metric_columns[1].metric("Sharpe", _fmt_num(metrics.get("sharpe", 0.0)))
    metric_columns[2].metric("Sortino", _fmt_num(metrics.get("sortino", 0.0)))
    metric_columns[3].metric("Max Drawdown", _fmt_pct(metrics.get("max_drawdown", 0.0)))
    metric_columns[4].metric("Expectancy", _fmt_pct(metrics.get("expectancy", 0.0)))
    metric_columns[5].metric("Trades", str(int(metrics.get("trade_count", 0))))

    if 'modeled_stop_risk_dollars' in equity_curve:
        risk_values = pd.to_numeric(equity_curve['modeled_stop_risk_dollars'], errors='coerce')
        finite_risk = risk_values.replace([float('inf'), -float('inf')], float('nan')).dropna()
        if not finite_risk.empty:
            st.caption(f"Largest modeled loss from current prices to stops: ${finite_risk.max():,.2f}. Gaps and execution costs can exceed this estimate.")
        if risk_values.isna().any() or risk_values.isin([float('inf'), -float('inf')]).any():
            st.warning("Stop-risk estimates are unavailable for some sessions; check missing prices and stop settings.")

    left, right = st.columns([1.1, 1.4])
    with left:
        st.subheader("Ranked Opportunities")
        if opportunities.empty:
            st.info("No opportunity ranking artifacts found.")
        else:
            view_columns = [
                column
                for column in [
                    "symbol",
                    "asset_class",
                    "direction",
                    "current_state",
                    "state_family",
                    "expected_value",
                    "expected_return",
                    "confidence",
                    "state_quality_score",
                    "transition_quality_score",
                    "fold_consistency_score",
                    "gate_passed",
                    "score",
                ]
                if column in opportunities.columns
            ]
            view = opportunities[view_columns].copy()
            st.dataframe(view, use_container_width=True, height=520)

    with right:
        st.subheader("Portfolio Equity Curve")
        if equity_curve.empty:
            st.info("No backtest equity curve available.")
        else:
            st.plotly_chart(
                _line_chart(
                    equity_curve.index,
                    equity_curve["equity"],
                    name="Equity",
                    color="#3982c6",
                    height=380,
                ),
                use_container_width=True,
            )

    lower_left, lower_right = st.columns([1.0, 1.0])
    with lower_left:
        st.subheader("Current State Distribution")
        if current_state_distribution.empty:
            st.info("No current-state distribution available.")
        else:
            st.plotly_chart(
                _grouped_bar_chart(
                    current_state_distribution,
                    x="current_state",
                    y="count",
                    group="asset_class",
                    y_title="Assets",
                    height=340,
                ),
                use_container_width=True,
            )
    with lower_right:
        st.subheader("State-Model Comparison Snapshot")
        if comparison_summary.empty:
            st.info("No state-model comparison artifacts found.")
        else:
            preview_columns = [
                column
                for column in [
                    "model_name",
                    "tradability_score",
                    "mean_test_sharpe",
                    "mean_test_total_return",
                    "state_coverage",
                    "transition_stability",
                ]
                if column in comparison_summary.columns
            ]
            st.dataframe(
                comparison_summary[preview_columns].head(5),
                use_container_width=True,
                height=340,
            )


def _render_symbol_detail(base: dict[str, object], artifacts_dir: Path) -> None:
    opportunities = base["opportunities"]
    state_diagnostics = base["state_diagnostics"]
    symbols = opportunities["symbol"].tolist() if not opportunities.empty else _symbol_list(artifacts_dir / "symbols")
    symbols = sorted(set(symbols).union(state_diagnostics.keys()))
    if not symbols:
        st.info("No symbol-level artifacts available.")
        return

    selected_symbol = st.selectbox("Symbol Detail", symbols, index=0)

    symbol_frame = _read_parquet(artifacts_dir / "symbols" / f"{selected_symbol}.parquet")
    transition_matrix = _read_csv(artifacts_dir / "markov" / f"{selected_symbol}_transition_matrix.csv", index_col=0)
    state_returns = _read_csv(artifacts_dir / "markov" / f"{selected_symbol}_state_returns.csv", index_col=0)
    confusion_matrix = _read_csv(artifacts_dir / "markov" / f"{selected_symbol}_confusion_matrix.csv", index_col=0)
    diagnostics = _read_json(artifacts_dir / "markov" / f"{selected_symbol}_diagnostics.json")

    if symbol_frame.empty:
        st.info("Selected symbol has no stored research frame.")
        return

    valid_rows = symbol_frame.dropna(subset=["state"])
    if valid_rows.empty:
        st.info("Selected symbol has no classified state history.")
        return
    latest_row = valid_rows.iloc[-1]

    summary_columns = st.columns(5)
    summary_columns[0].metric("Current State", str(latest_row["state"]))
    summary_columns[1].metric("Latest Signal", str(latest_row.get("signal", "flat")).upper())
    summary_columns[2].metric("Expected Value", _fmt_pct(latest_row.get("expected_value", 0.0)))
    summary_columns[3].metric("Confidence", _fmt_pct(latest_row.get("signal_confidence", 0.0)))
    summary_columns[4].metric("Transition Accuracy", _fmt_pct(diagnostics.get("accuracy", 0.0)))

    price_col, markov_col = st.columns([1.4, 1.0])
    with price_col:
        st.subheader(f"{selected_symbol} Price and Signals")
        figure = go.Figure()
        figure.add_trace(
            go.Scatter(
                x=symbol_frame.index,
                y=symbol_frame["close"],
                mode="lines",
                name="Close",
                line={"color": "#111827", "width": 2},
            )
        )
        if "signal" in symbol_frame.columns:
            longs = symbol_frame[symbol_frame["signal"] == "long"]
            shorts = symbol_frame[symbol_frame["signal"] == "short"]
            if not longs.empty:
                figure.add_trace(
                    go.Scatter(
                        x=longs.index,
                        y=longs["close"],
                        mode="markers",
                        name="Long",
                        marker={"color": "#0f766e", "size": 7, "symbol": "triangle-up"},
                    )
                )
            if not shorts.empty:
                figure.add_trace(
                    go.Scatter(
                        x=shorts.index,
                        y=shorts["close"],
                        mode="markers",
                        name="Short",
                        marker={"color": "#b91c1c", "size": 7, "symbol": "triangle-down"},
                    )
                )
        figure.update_layout(
            margin={"l": 10, "r": 10, "t": 10, "b": 10},
            height=420,
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(figure, use_container_width=True)

    with markov_col:
        st.subheader("Transition Matrix")
        if transition_matrix.empty:
            st.info("No transition matrix available.")
        else:
            st.plotly_chart(_heatmap(transition_matrix, height=420), use_container_width=True)

    table_col_a, table_col_b = st.columns(2)
    with table_col_a:
        st.subheader("State Return Profile")
        st.dataframe(state_returns, use_container_width=True, height=300)
    with table_col_b:
        st.subheader("Transition Confusion Matrix")
        st.dataframe(confusion_matrix, use_container_width=True, height=300)


def _render_state_diagnostics(base: dict[str, object]) -> None:
    diagnostics_by_symbol = base["state_diagnostics"]
    if not diagnostics_by_symbol:
        st.info("No state diagnostics found. Run `scripts/run_research.py` to generate per-symbol state diagnostics.")
        return

    symbols = sorted(diagnostics_by_symbol)
    selected_symbol = st.selectbox("State Diagnostics Symbol", symbols, index=0)
    diagnostics = diagnostics_by_symbol[selected_symbol]

    summary = diagnostics.get("summary", {})
    state_frequency = diagnostics.get("state_frequency", pd.DataFrame())
    state_persistence = diagnostics.get("state_persistence", pd.DataFrame())
    state_quality = diagnostics.get("state_quality", pd.DataFrame())
    transition_quality = diagnostics.get("transition_quality", pd.DataFrame())
    state_tradability = diagnostics.get("state_tradability", pd.DataFrame())
    sparse_states = diagnostics.get("sparse_states", pd.DataFrame())
    extra_artifacts = {
        name: artifact for name, artifact in diagnostics.items() if name not in CORE_STATE_ARTIFACTS
    }

    metric_columns = st.columns(5)
    metric_columns[0].metric("Unique States", str(int(summary.get("unique_states", 0))))
    metric_columns[1].metric("Sparse States", str(int(summary.get("sparse_state_count", 0))))
    metric_columns[2].metric("Sparse Ratio", _fmt_pct(summary.get("sparse_state_ratio", 0.0)))
    metric_columns[3].metric("Transition Stability", _fmt_num(summary.get("transition_stability", 0.0)))
    metric_columns[4].metric("Mean Persistence", _fmt_num(summary.get("mean_state_persistence", 0.0)))

    left, right = st.columns([1.0, 1.0])
    with left:
        st.subheader("State Frequency")
        if state_frequency.empty:
            st.info("No state-frequency data available.")
        else:
            frequency_view = state_frequency.sort_values("observations", ascending=False).head(20)
            st.plotly_chart(
                _bar_chart(
                    frequency_view,
                    x="state",
                    y="observations",
                    color="#3982c6",
                    y_title="Observations",
                    height=340,
                ),
                use_container_width=True,
            )
            st.dataframe(state_frequency, use_container_width=True, height=260)

    with right:
        st.subheader("State Persistence")
        if state_persistence.empty:
            st.info("No state-persistence data available.")
        else:
            persistence_view = state_persistence.sort_values("mean_duration", ascending=False).head(20)
            st.plotly_chart(
                _bar_chart(
                    persistence_view,
                    x="state",
                    y="mean_duration",
                    color="#966919",
                    y_title="Mean Duration",
                    height=340,
                ),
                use_container_width=True,
            )
            st.dataframe(state_persistence, use_container_width=True, height=260)

    lower_left, lower_right = st.columns([1.0, 1.0])
    with lower_left:
        st.subheader("State Quality")
        st.dataframe(state_quality, use_container_width=True, height=320)
    with lower_right:
        st.subheader("Transition Quality")
        st.dataframe(transition_quality, use_container_width=True, height=320)

    st.subheader("Sparse-State Warnings")
    if sparse_states.empty:
        st.info("No sparse-state warnings for this symbol.")
    else:
        st.dataframe(sparse_states, use_container_width=True, height=220)

    st.subheader("State Tradability")
    st.dataframe(state_tradability, use_container_width=True, height=260)

    st.subheader("Model Diagnostic Artifacts")
    if not extra_artifacts:
        st.info("No model-specific diagnostic artifacts were saved for this symbol.")
    else:
        artifact_name = st.selectbox(
            "Model Artifact",
            sorted(extra_artifacts),
            index=0,
            key="state_diag_artifact",
        )
        artifact = extra_artifacts[artifact_name]
        if isinstance(artifact, pd.DataFrame):
            st.dataframe(artifact, use_container_width=True, height=320)
        else:
            st.json(artifact)


def _render_walk_forward(research: dict[str, object]) -> None:
    aggregate = research["walk_aggregate"]
    fold_metrics = research["walk_fold_metrics"]
    trades = research["walk_trades"]

    if aggregate.empty and fold_metrics.empty:
        st.info("No walk-forward artifacts found. Run `scripts/run_walk_forward.py` or `scripts/run_robustness.py`.")
        return

    st.subheader("Aggregate Out-of-Sample Metrics")
    st.dataframe(aggregate, use_container_width=True, height=180)

    if not fold_metrics.empty:
        figure = go.Figure()
        for split, split_frame in fold_metrics.groupby("split"):
            figure.add_trace(
                go.Bar(
                    x=split_frame["fold_id"].astype(str),
                    y=split_frame["total_return"],
                    name=f"{split.title()} Return",
                )
            )
        figure.update_layout(
            barmode="group",
            height=360,
            margin={"l": 10, "r": 10, "t": 10, "b": 10},
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
            xaxis_title="Fold",
            yaxis_title="Total Return",
        )
        st.plotly_chart(figure, use_container_width=True)

    fold_col, trade_col = st.columns([1.2, 1.0])
    with fold_col:
        st.subheader("Fold-Level Metrics")
        st.dataframe(fold_metrics, use_container_width=True, height=340)
    with trade_col:
        st.subheader("Walk-Forward Trade Counts")
        if trades.empty:
            st.info("No walk-forward trades available.")
        else:
            counts = trades.groupby(["split", "asset_class"]).size().reset_index(name="trade_count")
            st.dataframe(counts, use_container_width=True, height=340)


def _render_edge_quality(base: dict[str, object], research: dict[str, object]) -> None:
    walk_quality = research["walk_quality"]
    if walk_quality:
        split_options = sorted(walk_quality)
        selected_split = st.selectbox("Quality Split", split_options, index=0, key="quality_split")
        quality = walk_quality[selected_split]
    else:
        quality = {
            key: base.get(key, pd.DataFrame())
            for key in [
                "state_quality_summary",
                "transition_quality_summary",
                "state_fold_consistency",
                "transition_fold_consistency",
                "state_family_performance",
                "state_family_fold_consistency",
                "best_states",
                "worst_states",
                "best_transitions",
                "worst_transitions",
                "signal_candidates",
                "gate_rejection_summary",
                "gate_rejection_by_asset_class",
                "gate_rejection_by_fold",
                "gate_starvation_summary",
                "quality_score_distributions",
                "score_component_summary",
                "score_component_distributions",
                "hard_filter_summary",
                "family_filter_summary",
                "top_n_selection_summary",
            ]
        }
        if not any(isinstance(frame, pd.DataFrame) and not frame.empty for frame in quality.values()):
            st.info("No edge-quality artifacts found. Run `scripts/run_walk_forward.py` or `scripts/run_research.py`.")
            return

    state_summary = quality.get("state_quality_summary", pd.DataFrame())
    transition_summary = quality.get("transition_quality_summary", pd.DataFrame())
    state_fold_consistency = quality.get("state_fold_consistency", pd.DataFrame())
    transition_fold_consistency = quality.get("transition_fold_consistency", pd.DataFrame())
    family_performance = quality.get("state_family_performance", pd.DataFrame())
    family_fold_consistency = quality.get("state_family_fold_consistency", pd.DataFrame())
    best_states = quality.get("best_states", pd.DataFrame())
    worst_states = quality.get("worst_states", pd.DataFrame())
    best_transitions = quality.get("best_transitions", pd.DataFrame())
    worst_transitions = quality.get("worst_transitions", pd.DataFrame())
    signal_candidates = quality.get("signal_candidates", pd.DataFrame())
    gate_rejection_summary = quality.get("gate_rejection_summary", pd.DataFrame())
    gate_rejection_by_asset = quality.get("gate_rejection_by_asset_class", pd.DataFrame())
    gate_rejection_by_fold = quality.get("gate_rejection_by_fold", pd.DataFrame())
    gate_starvation_summary = quality.get("gate_starvation_summary", pd.DataFrame())
    score_distributions = quality.get("quality_score_distributions", pd.DataFrame())
    score_component_summary = quality.get("score_component_summary", pd.DataFrame())
    score_component_distributions = quality.get("score_component_distributions", pd.DataFrame())
    hard_filter_summary = quality.get("hard_filter_summary", pd.DataFrame())
    family_filter_summary = quality.get("family_filter_summary", pd.DataFrame())
    top_n_selection_summary = quality.get("top_n_selection_summary", pd.DataFrame())

    metrics = st.columns(4)
    metrics[0].metric("Qualified States", str(int(len(state_summary))))
    metrics[1].metric("Qualified Transitions", str(int(len(transition_summary))))
    metrics[2].metric(
        "Positive-Expectancy States",
        str(int((state_summary.get("expectancy", pd.Series(dtype=float)) > 0).sum())),
    )
    metrics[3].metric(
        "Positive-Expectancy Transitions",
        str(int((transition_summary.get("expectancy", pd.Series(dtype=float)) > 0).sum())),
    )

    state_left, state_right = st.columns(2)
    with state_left:
        st.subheader("Best States")
        st.dataframe(best_states, use_container_width=True, height=280)
    with state_right:
        st.subheader("Worst States")
        st.dataframe(worst_states, use_container_width=True, height=280)

    transition_left, transition_right = st.columns(2)
    with transition_left:
        st.subheader("Best Transitions")
        st.dataframe(best_transitions, use_container_width=True, height=280)
    with transition_right:
        st.subheader("Worst Transitions")
        st.dataframe(worst_transitions, use_container_width=True, height=280)

    consistency_left, consistency_right = st.columns(2)
    with consistency_left:
        st.subheader("State Fold Consistency")
        st.dataframe(state_fold_consistency, use_container_width=True, height=260)
    with consistency_right:
        st.subheader("Transition Fold Consistency")
        st.dataframe(transition_fold_consistency, use_container_width=True, height=260)

    family_left, family_right = st.columns([1.1, 1.0])
    with family_left:
        st.subheader("State-Family Performance")
        st.dataframe(family_performance, use_container_width=True, height=260)
    with family_right:
        st.subheader("State-Family Fold Consistency")
        st.dataframe(family_fold_consistency, use_container_width=True, height=260)

    if not state_summary.empty and {"state", "quality_score"}.issubset(state_summary.columns):
        st.subheader("Top State Quality Scores")
        state_view = state_summary.head(15)
        st.plotly_chart(
            _bar_chart(
                state_view,
                x="state",
                y="quality_score",
                color="#3982c6",
                y_title="Quality Score",
                height=320,
            ),
            use_container_width=True,
        )

    breakdown_left, breakdown_right = st.columns(2)
    with breakdown_left:
        st.subheader("Gate Rejection Breakdown")
        st.dataframe(gate_rejection_summary, use_container_width=True, height=260)
    with breakdown_right:
        st.subheader("Most Starving Gates")
        st.dataframe(gate_starvation_summary, use_container_width=True, height=260)

    hard_left, hard_right = st.columns(2)
    with hard_left:
        st.subheader("Hard-Filter Breakdown")
        st.dataframe(hard_filter_summary, use_container_width=True, height=260)
    with hard_right:
        st.subheader("State-Family Filter Summary")
        st.dataframe(family_filter_summary, use_container_width=True, height=260)

    st.subheader("Gate Rejection by Asset Class")
    st.dataframe(gate_rejection_by_asset, use_container_width=True, height=220)

    st.subheader("Gate Rejection by Fold")
    st.dataframe(gate_rejection_by_fold, use_container_width=True, height=220)

    distribution_left, distribution_right = st.columns([1.2, 1.0])
    with distribution_left:
        st.subheader("Quality-Score Distributions")
        if score_distributions.empty:
            st.info("No quality-score distribution data available.")
        else:
            distribution_metric = st.selectbox(
                "Distribution Metric",
                sorted(score_distributions["metric"].dropna().unique().tolist()),
                index=0,
                key="quality_distribution_metric",
            )
            distribution_view = score_distributions[score_distributions["metric"] == distribution_metric]
            if not distribution_view.empty and "score_bin" in distribution_view.columns:
                st.plotly_chart(
                    _grouped_bar_chart(
                        distribution_view,
                        x="score_bin",
                        y="count",
                        group="asset_class" if "asset_class" in distribution_view.columns else "metric",
                        y_title="Candidates",
                        height=320,
                    ),
                    use_container_width=True,
                )
    with distribution_right:
        st.subheader("Top-N Selection Summary")
        st.dataframe(top_n_selection_summary, use_container_width=True, height=320)

    component_left, component_right = st.columns([1.1, 1.0])
    with component_left:
        st.subheader("Score-Component Summary")
        st.dataframe(score_component_summary, use_container_width=True, height=320)
    with component_right:
        st.subheader("Component Distributions")
        st.dataframe(score_component_distributions, use_container_width=True, height=320)

    st.subheader("Signal Candidates")
    st.dataframe(signal_candidates, use_container_width=True, height=280)


def _render_gate_ablation(research: dict[str, object]) -> None:
    summary = research["ablation_summary"]
    fold_metrics = research["ablation_fold_metrics"]
    outputs = research["ablation_outputs"]

    if summary.empty:
        st.info("No gate-ablation artifacts found. Run `scripts/run_gate_ablation.py`.")
        return

    metrics = st.columns(4)
    metrics[0].metric("Best Combo", str(summary.iloc[0].get("combo_name", "")))
    metrics[1].metric("Best Ablation Score", _fmt_num(summary.iloc[0].get("ablation_score", 0.0)))
    metrics[2].metric("Best Test Sharpe", _fmt_num(summary.iloc[0].get("mean_test_sharpe", 0.0)))
    metrics[3].metric("Best Trade Count", _fmt_num(summary.iloc[0].get("mean_test_trade_count", 0.0)))

    st.subheader("Gate-Ablation Summary")
    st.dataframe(summary, use_container_width=True, height=260)

    if not summary.empty and {"model_name", "ablation_name", "ablation_score"}.issubset(summary.columns):
        pivot = summary.pivot_table(index="model_name", columns="ablation_name", values="ablation_score", aggfunc="mean")
        st.plotly_chart(_heatmap(pivot, height=320), use_container_width=True)

    if not fold_metrics.empty:
        split_options = sorted(fold_metrics["split"].dropna().unique().tolist())
        split_choice = st.selectbox("Ablation Split", split_options, index=0, key="ablation_split")
        fold_metric_choice = st.selectbox(
            "Ablation Fold Metric",
            [column for column in ["total_return", "sharpe", "expectancy", "trade_count"] if column in fold_metrics.columns],
            index=0,
            key="ablation_fold_metric",
        )
        split_frame = fold_metrics[fold_metrics["split"] == split_choice]
        figure = go.Figure()
        for combo_name, combo_frame in split_frame.groupby("combo_name"):
            figure.add_trace(go.Box(y=combo_frame[fold_metric_choice], name=combo_name, boxmean=True))
        figure.update_layout(
            height=320,
            margin={"l": 10, "r": 10, "t": 10, "b": 10},
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
            yaxis_title=fold_metric_choice,
        )
        st.plotly_chart(figure, use_container_width=True)

    selected_combo = st.selectbox("Inspect Ablation Combo", summary["combo_name"].tolist(), index=0, key="ablation_combo")
    selected_outputs = outputs.get(selected_combo, {})
    detail_left, detail_right = st.columns(2)
    with detail_left:
        st.subheader("Gate Rejection Breakdown")
        st.dataframe(selected_outputs.get("test_gate_rejection_summary", pd.DataFrame()), use_container_width=True, height=260)
    with detail_right:
        st.subheader("Top-N Selection Summary")
        st.dataframe(selected_outputs.get("test_top_n_selection_summary", pd.DataFrame()), use_container_width=True, height=260)


def _render_selection_policy_comparison(research: dict[str, object]) -> None:
    summary = research["policy_summary"]
    fold_metrics = research["policy_fold_metrics"]
    outputs = research["policy_outputs"]

    if summary.empty:
        st.info("No selection-policy comparison artifacts found. Run `scripts/run_selection_policy_comparison.py`.")
        return

    metrics = st.columns(4)
    metrics[0].metric("Best Policy", str(summary.iloc[0].get("experiment_name", "")))
    metrics[1].metric("Best Policy Score", _fmt_num(summary.iloc[0].get("policy_score", 0.0)))
    metrics[2].metric("Best Test Sharpe", _fmt_num(summary.iloc[0].get("mean_test_sharpe", 0.0)))
    metrics[3].metric("Post-Cap Test Trades", _fmt_num(summary.iloc[0].get("post_cap_trade_count_test", 0.0)))

    st.subheader("Model-Specific Policy Comparison")
    st.dataframe(summary, use_container_width=True, height=280)

    family_subset = summary[summary["category"] == "family_filter"] if "category" in summary.columns else pd.DataFrame()
    topn_subset = summary[summary["category"] == "top_n"] if "category" in summary.columns else pd.DataFrame()

    family_left, family_right = st.columns(2)
    with family_left:
        st.subheader("Family-Filter Ablations")
        st.dataframe(family_subset, use_container_width=True, height=240)
    with family_right:
        st.subheader("Top-N Policy Variants")
        st.dataframe(topn_subset, use_container_width=True, height=240)

    if not fold_metrics.empty:
        split_options = sorted(fold_metrics["split"].dropna().unique().tolist())
        split_choice = st.selectbox("Policy Split", split_options, index=0, key="policy_split")
        fold_metric_choice = st.selectbox(
            "Policy Fold Metric",
            [column for column in ["total_return", "sharpe", "expectancy", "trade_count"] if column in fold_metrics.columns],
            index=0,
            key="policy_fold_metric",
        )
        split_frame = fold_metrics[fold_metrics["split"] == split_choice]
        figure = go.Figure()
        for experiment_name, experiment_frame in split_frame.groupby("experiment_name"):
            figure.add_trace(go.Box(y=experiment_frame[fold_metric_choice], name=experiment_name, boxmean=True))
        figure.update_layout(
            height=320,
            margin={"l": 10, "r": 10, "t": 10, "b": 10},
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
            yaxis_title=fold_metric_choice,
        )
        st.plotly_chart(figure, use_container_width=True)

    selected_experiment = st.selectbox(
        "Inspect Policy Experiment",
        summary["experiment_name"].tolist(),
        index=0,
        key="policy_experiment",
    )
    selected_outputs = outputs.get(selected_experiment, {})

    detail_left, detail_right = st.columns(2)
    with detail_left:
        st.subheader("Hard-Filter Summary")
        st.dataframe(selected_outputs.get("test_hard_filter_summary", pd.DataFrame()), use_container_width=True, height=240)
    with detail_right:
        st.subheader("Family-Filter Summary")
        st.dataframe(selected_outputs.get("test_family_filter_summary", pd.DataFrame()), use_container_width=True, height=240)

    score_left, score_right = st.columns(2)
    with score_left:
        st.subheader("Score-Component Summary")
        st.dataframe(selected_outputs.get("test_score_component_summary", pd.DataFrame()), use_container_width=True, height=240)
    with score_right:
        st.subheader("Score-Component Distributions")
        st.dataframe(selected_outputs.get("test_score_component_distributions", pd.DataFrame()), use_container_width=True, height=240)

    cap_left, cap_right = st.columns(2)
    with cap_left:
        st.subheader("Top-N Selection Summary")
        st.dataframe(selected_outputs.get("test_top_n_selection_summary", pd.DataFrame()), use_container_width=True, height=220)
    with cap_right:
        st.subheader("Gate Rejection Breakdown")
        st.dataframe(selected_outputs.get("test_gate_rejection_summary", pd.DataFrame()), use_container_width=True, height=220)


def _render_exit_research(research: dict[str, object]) -> None:
    summary = research["exit_summary"]
    fold_metrics = research["exit_fold_metrics"]
    combo_outputs = research["exit_outputs"]

    if summary.empty:
        st.info("No exit-comparison artifacts found. Run `scripts/run_exit_comparison.py`.")
        return

    metrics = st.columns(4)
    metrics[0].metric("Best Combo", str(summary.iloc[0].get("combo_name", "")))
    metrics[1].metric("Best Exit Score", _fmt_num(summary.iloc[0].get("exit_score", 0.0)))
    metrics[2].metric("Best Test Sharpe", _fmt_num(summary.iloc[0].get("mean_test_sharpe", 0.0)))
    metrics[3].metric("Best Runtime (s)", _fmt_num(summary.iloc[0].get("total_runtime_seconds", 0.0)))

    st.subheader("Model vs Exit Summary")
    st.dataframe(summary, use_container_width=True, height=260)

    metric_options = [
        column
        for column in [
            "exit_score",
            "mean_test_sharpe",
            "mean_test_total_return",
            "mean_test_expectancy",
            "mean_test_trade_count",
            "positive_fold_rate_test",
        ]
        if column in summary.columns
    ]
    if metric_options and {"model_name", "exit_profile"}.issubset(summary.columns):
        heatmap_metric = st.selectbox("Exit Heatmap Metric", metric_options, index=0, key="exit_heatmap_metric")
        pivot = summary.pivot_table(index="model_name", columns="exit_profile", values=heatmap_metric, aggfunc="mean")
        st.plotly_chart(_heatmap(pivot, height=320), use_container_width=True)

    runtime_columns = [column for column in ["prepare_seconds", "run_seconds", "total_runtime_seconds"] if column in summary.columns]
    if runtime_columns:
        st.subheader("Exit Runtime Diagnostics")
        st.dataframe(summary[["combo_name", *runtime_columns]], use_container_width=True, height=220)

    if not fold_metrics.empty:
        split_options = sorted(fold_metrics["split"].dropna().unique().tolist())
        split_choice = st.selectbox("Exit Split", split_options, index=0, key="exit_split")
        fold_metric_choice = st.selectbox(
            "Exit Fold Metric",
            [column for column in ["total_return", "sharpe", "expectancy", "trade_count"] if column in fold_metrics.columns],
            index=0,
            key="exit_fold_metric",
        )
        split_frame = fold_metrics[fold_metrics["split"] == split_choice]
        figure = go.Figure()
        for combo_name, combo_frame in split_frame.groupby("combo_name"):
            figure.add_trace(go.Box(y=combo_frame[fold_metric_choice], name=combo_name, boxmean=True))
        figure.update_layout(
            height=320,
            margin={"l": 10, "r": 10, "t": 10, "b": 10},
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
            yaxis_title=fold_metric_choice,
        )
        st.plotly_chart(figure, use_container_width=True)

    selected_combo = st.selectbox("Inspect Exit Combo", summary["combo_name"].tolist(), index=0, key="exit_combo")
    outputs = combo_outputs.get(selected_combo, {})
    combo_left, combo_right = st.columns([1.0, 1.0])
    with combo_left:
        st.subheader("Aggregate Metrics")
        st.dataframe(outputs.get("aggregate_metrics", pd.DataFrame()), use_container_width=True, height=220)
    with combo_right:
        st.subheader("Runtime Summary")
        st.dataframe(outputs.get("runtime_summary", pd.DataFrame()), use_container_width=True, height=220)

    detail_left, detail_right = st.columns(2)
    with detail_left:
        st.subheader("Best States")
        st.dataframe(outputs.get("test_best_states", pd.DataFrame()), use_container_width=True, height=260)
    with detail_right:
        st.subheader("Best Transitions")
        st.dataframe(outputs.get("test_best_transitions", pd.DataFrame()), use_container_width=True, height=260)


def _render_parameter_sweeps(research: dict[str, object]) -> None:
    summary = research["sweep_summary"]
    fold_metrics = research["sweep_fold_metrics"]

    if summary.empty:
        st.info("No parameter sweep artifacts found. Run `scripts/run_parameter_sweep.py` or `scripts/run_robustness.py`.")
        return

    st.subheader("Sweep Rankings")
    ordered = summary.sort_values("rank") if "rank" in summary.columns else summary
    st.dataframe(ordered, use_container_width=True, height=340)

    param_columns = [column for column in summary.columns if column.startswith("param_")]
    metric_columns = [column for column in summary.columns if column.startswith("mean_")]
    if len(param_columns) >= 2 and metric_columns:
        heatmap_col, control_col = st.columns([1.4, 1.0])
        with control_col:
            x_axis = st.selectbox("Heatmap X", param_columns, index=0, key="sweep_x")
            y_axis = st.selectbox("Heatmap Y", param_columns, index=1, key="sweep_y")
            metric = st.selectbox(
                "Heatmap Metric",
                metric_columns,
                index=metric_columns.index("mean_test_sharpe") if "mean_test_sharpe" in metric_columns else 0,
                key="sweep_metric",
            )
        with heatmap_col:
            pivot = summary.pivot_table(index=y_axis, columns=x_axis, values=metric, aggfunc="mean")
            st.plotly_chart(_heatmap(pivot, height=420), use_container_width=True)

    st.subheader("Sweep Fold Metrics")
    st.dataframe(fold_metrics, use_container_width=True, height=300)


def _render_state_model_comparison(research: dict[str, object]) -> None:
    summary = research["comparison_summary"]
    fold_metrics = research["comparison_fold_metrics"]
    sensitivity = research["comparison_sensitivity"]
    model_outputs = research["comparison_models"]

    if summary.empty:
        st.info("No state-model comparison artifacts found. Run `scripts/run_state_model_comparison.py`.")
        return

    best_model = summary.iloc[0]["model_name"]
    metric_columns = st.columns(5)
    metric_columns[0].metric("Best Model", str(best_model))
    metric_columns[1].metric("Tradability Score", _fmt_num(summary.iloc[0].get("tradability_score", 0.0)))
    metric_columns[2].metric("Best Test Sharpe", _fmt_num(summary.iloc[0].get("mean_test_sharpe", 0.0)))
    metric_columns[3].metric("Effective Coverage", _fmt_pct(summary.iloc[0].get("effective_state_coverage", 0.0)))
    metric_columns[4].metric("Trade-Generating States", _fmt_pct(summary.iloc[0].get("trade_generating_state_percentage", 0.0)))

    st.subheader("Model Comparison Summary")
    st.dataframe(summary, use_container_width=True, height=280)

    compare_col, scatter_col = st.columns([1.2, 1.0])
    comparison_metrics = [
        column
        for column in [
            "tradability_score",
            "sparsity_score",
            "mean_test_sharpe",
            "mean_test_total_return",
            "mean_test_expectancy",
            "mean_test_trade_count",
            "state_coverage",
            "effective_state_coverage",
            "share_states_above_threshold",
            "trade_generating_state_percentage",
            "transition_stability",
            "mean_unique_states",
            "mean_state_persistence",
        ]
        if column in summary.columns
    ]
    with compare_col:
        selected_metric = st.selectbox("Comparison Metric", comparison_metrics, index=0, key="comparison_metric")
        st.plotly_chart(
            _bar_chart(
                summary.sort_values(selected_metric, ascending=False),
                x="model_name",
                y=selected_metric,
                color="#3982c6",
                y_title=selected_metric,
                height=340,
            ),
            use_container_width=True,
        )
    with scatter_col:
        if {"mean_unique_states", "mean_test_trade_count"}.issubset(summary.columns):
            figure = go.Figure(
                data=[
                    go.Scatter(
                        x=summary["mean_unique_states"],
                        y=summary["mean_test_trade_count"],
                        mode="markers+text",
                        text=summary["model_name"],
                        textposition="top center",
                        marker={
                            "size": 12,
                            "color": summary.get("tradability_score", pd.Series([0.0] * len(summary))),
                            "colorscale": "YlGnBu",
                            "showscale": True,
                        },
                    )
                ]
            )
            figure.update_layout(
                height=340,
                margin={"l": 10, "r": 10, "t": 10, "b": 10},
                plot_bgcolor="rgba(0,0,0,0)",
                paper_bgcolor="rgba(0,0,0,0)",
                xaxis_title="Mean Unique States",
                yaxis_title="Mean Test Trade Count",
            )
            st.plotly_chart(figure, use_container_width=True)
        else:
            st.info("State-count vs trade-count plot unavailable for current artifacts.")

    sparsity_metrics = [
        column
        for column in [
            "mean_unique_states",
            "mean_median_state_observations",
            "min_active_state_observations",
            "share_states_above_threshold",
            "effective_state_coverage",
            "trade_generating_state_percentage",
            "transition_stability",
            "transition_concentration",
            "tradability_score",
        ]
        if column in summary.columns
    ]
    if sparsity_metrics:
        st.subheader("Sparsity and Tradability Heatmap")
        heatmap_source = summary.set_index("model_name")[sparsity_metrics]
        st.plotly_chart(_heatmap(heatmap_source, height=360), use_container_width=True)

    if not fold_metrics.empty:
        st.subheader("Fold Sensitivity by Model")
        split_options = sorted(fold_metrics["split"].dropna().unique().tolist())
        split_choice = st.selectbox("Comparison Split", split_options, index=0, key="comparison_split")
        fold_metric_choice = st.selectbox(
            "Fold Metric",
            [column for column in ["total_return", "sharpe", "expectancy", "trade_count"] if column in fold_metrics.columns],
            index=0,
            key="comparison_fold_metric",
        )
        split_frame = fold_metrics[fold_metrics["split"] == split_choice]
        figure = go.Figure()
        for model_name, model_frame in split_frame.groupby("model_name"):
            figure.add_trace(
                go.Box(
                    y=model_frame[fold_metric_choice],
                    name=model_name,
                    boxmean=True,
                )
            )
        figure.update_layout(
            height=340,
            margin={"l": 10, "r": 10, "t": 10, "b": 10},
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
            yaxis_title=fold_metric_choice,
        )
        st.plotly_chart(figure, use_container_width=True)

    available_models = summary["model_name"].tolist()
    selected_model = st.selectbox("Inspect State Model", available_models, index=0, key="selected_state_model")
    selected_outputs = model_outputs.get(selected_model, {})

    model_left, model_right = st.columns([1.0, 1.0])
    with model_left:
        st.subheader("Selected Model Aggregate Metrics")
        st.dataframe(selected_outputs.get("aggregate_metrics", pd.DataFrame()), use_container_width=True, height=220)
    with model_right:
        st.subheader("Per-Symbol State Summary")
        st.dataframe(selected_outputs.get("state_symbol_summary", pd.DataFrame()), use_container_width=True, height=220)

    distribution = selected_outputs.get("current_state_distribution", pd.DataFrame())
    if not distribution.empty:
        st.subheader("Current State Distribution")
        st.plotly_chart(
            _grouped_bar_chart(
                distribution,
                x="current_state",
                y="count",
                group="asset_class",
                y_title="Assets",
                height=320,
            ),
            use_container_width=True,
        )

    detail_left, detail_right = st.columns([1.0, 1.0])
    with detail_left:
        st.subheader("State Persistence")
        st.dataframe(selected_outputs.get("state_persistence", pd.DataFrame()), use_container_width=True, height=300)
    with detail_right:
        st.subheader("State Quality")
        st.dataframe(selected_outputs.get("state_quality", pd.DataFrame()), use_container_width=True, height=300)

    st.subheader("Transition Quality")
    st.dataframe(selected_outputs.get("transition_quality", pd.DataFrame()), use_container_width=True, height=280)

    st.subheader("State Tradability")
    st.dataframe(selected_outputs.get("state_tradability", pd.DataFrame()), use_container_width=True, height=260)

    quality_left, quality_right = st.columns(2)
    with quality_left:
        st.subheader("Walk-Forward Best States")
        st.dataframe(selected_outputs.get("test_best_states", pd.DataFrame()), use_container_width=True, height=240)
    with quality_right:
        st.subheader("Walk-Forward Best Transitions")
        st.dataframe(selected_outputs.get("test_best_transitions", pd.DataFrame()), use_container_width=True, height=240)

    consistency_left, consistency_right = st.columns(2)
    with consistency_left:
        st.subheader("Walk-Forward State Fold Consistency")
        st.dataframe(selected_outputs.get("test_state_fold_consistency", pd.DataFrame()), use_container_width=True, height=220)
    with consistency_right:
        st.subheader("Walk-Forward State-Family Performance")
        st.dataframe(selected_outputs.get("test_state_family_performance", pd.DataFrame()), use_container_width=True, height=220)

    selection_left, selection_right = st.columns(2)
    with selection_left:
        st.subheader("Walk-Forward Gate Rejection")
        st.dataframe(selected_outputs.get("test_gate_rejection_summary", pd.DataFrame()), use_container_width=True, height=220)
    with selection_right:
        st.subheader("Walk-Forward Top-N Selection")
        st.dataframe(selected_outputs.get("test_top_n_selection_summary", pd.DataFrame()), use_container_width=True, height=220)

    selected_sensitivity = sensitivity[sensitivity["model_name"] == selected_model] if not sensitivity.empty else pd.DataFrame()
    st.subheader("Local Sensitivity")
    if selected_sensitivity.empty:
        st.info("No local sensitivity overrides were configured for this model.")
    else:
        st.dataframe(selected_sensitivity, use_container_width=True, height=220)

    model_specific_artifacts = {
        name: frame
        for name, frame in selected_outputs.items()
        if name
        not in {
            "aggregate_metrics",
            "state_symbol_summary",
            "state_frequency",
            "state_persistence",
            "state_quality",
            "transition_quality",
            "state_tradability",
            "current_state_distribution",
        }
    }
    cluster_artifacts = {name: frame for name, frame in model_specific_artifacts.items() if "cluster" in name or "pca_" in name}
    hybrid_artifacts = {name: frame for name, frame in model_specific_artifacts.items() if name.startswith("hybrid_")}

    if cluster_artifacts:
        st.subheader("Cluster Diagnostics")
        for artifact_name in sorted(cluster_artifacts):
            st.markdown(f"**{artifact_name}**")
            st.dataframe(cluster_artifacts[artifact_name], use_container_width=True, height=220)

    if hybrid_artifacts:
        st.subheader("Hybrid-State Summaries")
        for artifact_name in sorted(hybrid_artifacts):
            st.markdown(f"**{artifact_name}**")
            st.dataframe(hybrid_artifacts[artifact_name], use_container_width=True, height=220)

    st.subheader("Model Diagnostic Tables")
    if not model_specific_artifacts:
        st.info("No model-specific diagnostic tables were saved for this comparison run.")
    else:
        artifact_name = st.selectbox(
            "Comparison Artifact",
            sorted(model_specific_artifacts),
            index=0,
            key="comparison_artifact",
        )
        st.dataframe(model_specific_artifacts[artifact_name], use_container_width=True, height=320)


def _render_state_attribution(base: dict[str, object], research: dict[str, object]) -> None:
    source = "walk_forward" if research["walk_attribution"] else "baseline"
    split_options = sorted(research["walk_attribution"].keys()) if research["walk_attribution"] else ["baseline"]
    selected_split = st.selectbox("Attribution Split", split_options, index=0)

    if source == "walk_forward":
        attribution = research["walk_attribution"][selected_split]
        state_edge = attribution.get("state_edge_summary", pd.DataFrame())
        transition_edge = attribution.get("transition_edge_summary", pd.DataFrame())
        vol_perf = attribution.get("volatility_regime_performance", pd.DataFrame())
        asset_perf = attribution.get("asset_class_performance", pd.DataFrame())
    else:
        state_edge = base["state_edge_summary"]
        transition_edge = base["transition_edge_summary"]
        vol_perf = base["performance_by_volatility_regime"]
        asset_perf = base["performance_by_asset_class"]

    row_a, row_b = st.columns(2)
    with row_a:
        st.subheader("State Edge Summary")
        st.dataframe(state_edge, use_container_width=True, height=320)
    with row_b:
        st.subheader("Transition Edge Summary")
        st.dataframe(transition_edge, use_container_width=True, height=320)

    row_c, row_d = st.columns(2)
    with row_c:
        st.subheader("Performance by Volatility Regime")
        st.dataframe(vol_perf, use_container_width=True, height=260)
    with row_d:
        st.subheader("Performance by Asset Class")
        st.dataframe(asset_perf, use_container_width=True, height=260)


def _render_failure_analysis(base: dict[str, object], research: dict[str, object]) -> None:
    source = "walk_forward" if research["walk_diagnostics"] else "baseline"
    split_options = sorted(research["walk_diagnostics"].keys()) if research["walk_diagnostics"] else ["baseline"]
    selected_split = st.selectbox("Diagnostic Split", split_options, index=0)

    if source == "walk_forward":
        diagnostics = research["walk_diagnostics"][selected_split]
        best_trades = diagnostics.get("best_trades", pd.DataFrame())
        worst_trades = diagnostics.get("worst_trades", pd.DataFrame())
        failure_modes = diagnostics.get("failure_modes", pd.DataFrame())
    else:
        best_trades = base["best_trades"]
        worst_trades = base["worst_trades"]
        failure_modes = base["failure_modes"]

    row_a, row_b = st.columns(2)
    with row_a:
        st.subheader("Worst Trades")
        st.dataframe(worst_trades, use_container_width=True, height=320)
    with row_b:
        st.subheader("Best Trades")
        st.dataframe(best_trades, use_container_width=True, height=320)

    st.subheader("Most Frequent Failure Modes")
    st.dataframe(failure_modes, use_container_width=True, height=320)


def _load_base_artifacts(artifacts_dir: Path) -> dict[str, object]:
    opportunities = _read_csv(artifacts_dir / "opportunities.csv")
    current_state_distribution = pd.DataFrame()
    if not opportunities.empty and {"asset_class", "current_state"}.issubset(opportunities.columns):
        current_state_distribution = (
            opportunities.groupby(["asset_class", "current_state"]).size().reset_index(name="count")
        )
    return {
        "opportunities": opportunities,
        "metrics": _read_json(artifacts_dir / "backtest" / "metrics.json"),
        "equity_curve": _read_csv(artifacts_dir / "backtest" / "equity_curve.csv", index_col=0, parse_dates=True),
        "trades": _read_csv(
            artifacts_dir / "backtest" / "trades.csv",
            parse_dates=["entry_timestamp", "exit_timestamp", "generated_at"],
        ),
        "performance_by_state": _read_csv(artifacts_dir / "analytics" / "performance_by_state.csv"),
        "performance_by_transition": _read_csv(artifacts_dir / "analytics" / "performance_by_transition.csv"),
        "performance_by_volatility_regime": _read_csv(
            artifacts_dir / "analytics" / "performance_by_volatility_regime.csv"
        ),
        "performance_by_asset_class": _read_csv(artifacts_dir / "analytics" / "performance_by_asset_class.csv"),
        "state_edge_summary": _read_csv(artifacts_dir / "analytics" / "state_edge_summary.csv"),
        "transition_edge_summary": _read_csv(artifacts_dir / "analytics" / "transition_edge_summary.csv"),
        "state_quality_summary": _read_csv(artifacts_dir / "analytics" / "state_quality_summary.csv"),
        "transition_quality_summary": _read_csv(artifacts_dir / "analytics" / "transition_quality_summary.csv"),
        "state_fold_consistency": _read_csv(artifacts_dir / "analytics" / "state_fold_consistency.csv"),
        "transition_fold_consistency": _read_csv(artifacts_dir / "analytics" / "transition_fold_consistency.csv"),
        "state_family_performance": _read_csv(artifacts_dir / "analytics" / "state_family_performance.csv"),
        "state_family_fold_consistency": _read_csv(artifacts_dir / "analytics" / "state_family_fold_consistency.csv"),
        "best_states": _read_csv(artifacts_dir / "analytics" / "best_states.csv"),
        "worst_states": _read_csv(artifacts_dir / "analytics" / "worst_states.csv"),
        "best_transitions": _read_csv(artifacts_dir / "analytics" / "best_transitions.csv"),
        "worst_transitions": _read_csv(artifacts_dir / "analytics" / "worst_transitions.csv"),
        "signal_candidates": _read_csv(artifacts_dir / "analytics" / "signal_candidates.csv"),
        "gate_rejection_summary": _read_csv(artifacts_dir / "analytics" / "gate_rejection_summary.csv"),
        "gate_rejection_by_asset_class": _read_csv(artifacts_dir / "analytics" / "gate_rejection_by_asset_class.csv"),
        "gate_rejection_by_fold": _read_csv(artifacts_dir / "analytics" / "gate_rejection_by_fold.csv"),
        "gate_starvation_summary": _read_csv(artifacts_dir / "analytics" / "gate_starvation_summary.csv"),
        "quality_score_distributions": _read_csv(artifacts_dir / "analytics" / "quality_score_distributions.csv"),
        "score_component_summary": _read_csv(artifacts_dir / "analytics" / "score_component_summary.csv"),
        "score_component_distributions": _read_csv(
            artifacts_dir / "analytics" / "score_component_distributions.csv"
        ),
        "hard_filter_summary": _read_csv(artifacts_dir / "analytics" / "hard_filter_summary.csv"),
        "family_filter_summary": _read_csv(artifacts_dir / "analytics" / "family_filter_summary.csv"),
        "top_n_selection_summary": _read_csv(artifacts_dir / "analytics" / "top_n_selection_summary.csv"),
        "best_trades": _read_csv(artifacts_dir / "analytics" / "best_trades.csv"),
        "worst_trades": _read_csv(artifacts_dir / "analytics" / "worst_trades.csv"),
        "failure_modes": _read_csv(artifacts_dir / "analytics" / "failure_modes.csv"),
        "current_state_distribution": current_state_distribution,
        "state_diagnostics": _load_symbol_state_artifacts(artifacts_dir / "state_diagnostics"),
    }


def _load_research_artifacts(artifacts_dir: Path) -> dict[str, object]:
    walk_attribution = _load_prefixed_frames(artifacts_dir / "research" / "walk_forward" / "attribution")
    walk_diagnostics = _load_prefixed_frames(artifacts_dir / "research" / "walk_forward" / "diagnostics")
    walk_quality = _load_prefixed_frames(artifacts_dir / "research" / "walk_forward" / "quality")
    comparison_root = artifacts_dir / "research" / "state_model_comparison"
    policy_root = artifacts_dir / "research" / "selection_policy_comparison"
    exit_root = artifacts_dir / "research" / "exit_comparison"
    ablation_root = artifacts_dir / "research" / "gate_ablation"
    return {
        "walk_folds": _read_csv(
            artifacts_dir / "research" / "walk_forward" / "folds.csv",
            parse_dates=["train_start", "train_end", "validation_start", "validation_end", "test_start", "test_end"],
        ),
        "walk_fold_metrics": _read_csv(artifacts_dir / "research" / "walk_forward" / "fold_metrics.csv"),
        "walk_aggregate": _read_csv(artifacts_dir / "research" / "walk_forward" / "aggregate_metrics.csv"),
        "walk_trades": _read_csv(
            artifacts_dir / "research" / "walk_forward" / "trades.csv",
            parse_dates=["entry_timestamp", "exit_timestamp", "generated_at"],
        ),
        "walk_diagnostics": walk_diagnostics,
        "walk_attribution": walk_attribution,
        "walk_quality": walk_quality,
        "sweep_summary": _read_csv(artifacts_dir / "research" / "parameter_sweep" / "summary.csv"),
        "sweep_fold_metrics": _read_csv(artifacts_dir / "research" / "parameter_sweep" / "fold_metrics.csv"),
        "comparison_summary": _read_csv(comparison_root / "summary.csv"),
        "comparison_fold_metrics": _read_csv(comparison_root / "fold_metrics.csv"),
        "comparison_sensitivity": _read_csv(comparison_root / "sensitivity_summary.csv"),
        "comparison_models": _load_model_comparison_outputs(comparison_root),
        "policy_summary": _read_csv(policy_root / "summary.csv"),
        "policy_fold_metrics": _read_csv(policy_root / "fold_metrics.csv"),
        "policy_outputs": _load_model_comparison_outputs(policy_root),
        "exit_summary": _read_csv(exit_root / "summary.csv"),
        "exit_fold_metrics": _read_csv(exit_root / "fold_metrics.csv"),
        "exit_outputs": _load_model_comparison_outputs(exit_root),
        "ablation_summary": _read_csv(ablation_root / "summary.csv"),
        "ablation_fold_metrics": _read_csv(ablation_root / "fold_metrics.csv"),
        "ablation_outputs": _load_model_comparison_outputs(ablation_root),
    }


def _load_prefixed_frames(directory: Path) -> dict[str, dict[str, pd.DataFrame]]:
    if not directory.exists():
        return {}
    grouped: dict[str, dict[str, pd.DataFrame]] = {}
    for path in sorted(directory.glob("*.csv")):
        stem = path.stem
        if "_" not in stem:
            continue
        split, name = stem.split("_", 1)
        grouped.setdefault(split, {})[name] = _read_csv(path)
    return grouped


def _load_symbol_state_artifacts(directory: Path) -> dict[str, dict[str, pd.DataFrame | dict]]:
    if not directory.exists():
        return {}
    grouped: dict[str, dict[str, pd.DataFrame | dict]] = {}
    known_suffixes = sorted(
        CORE_STATE_ARTIFACTS.union(
            {
                "model_settings",
                "feature_groups",
                "bin_edges",
                "sparse_merge_levels",
                "cluster_centers",
                "cluster_sizes",
                "cluster_merge_map",
                "cluster_model_settings",
                "pca_explained_variance",
                "context_settings",
                "hybrid_regime_summary",
                "hybrid_cluster_centers",
                "hybrid_cluster_sizes",
                "hybrid_model_settings",
            }
        ),
        key=len,
        reverse=True,
    )
    for path in sorted(directory.iterdir()):
        if path.suffix not in {".csv", ".json"}:
            continue
        symbol, artifact_name = _split_symbol_artifact(path.stem, known_suffixes)
        if not symbol or not artifact_name:
            continue
        grouped.setdefault(symbol, {})
        if path.suffix == ".csv":
            grouped[symbol][artifact_name] = _read_csv(path)
        else:
            grouped[symbol][artifact_name] = _read_json(path)
    return grouped


def _load_model_comparison_outputs(directory: Path) -> dict[str, dict[str, pd.DataFrame]]:
    if not directory.exists():
        return {}
    grouped: dict[str, dict[str, pd.DataFrame]] = {}
    for model_dir in sorted(directory.iterdir()):
        if not model_dir.is_dir():
            continue
        artifacts: dict[str, pd.DataFrame] = {}
        for path in sorted(model_dir.glob("*.csv")):
            artifacts[path.stem] = _read_csv(path)
        grouped[model_dir.name] = artifacts
    return grouped


def _split_symbol_artifact(stem: str, known_suffixes: list[str]) -> tuple[str | None, str | None]:
    for suffix in known_suffixes:
        token = f"_{suffix}"
        if stem.endswith(token):
            return stem[: -len(token)], suffix
    if "_" not in stem:
        return None, None
    symbol, artifact_name = stem.split("_", 1)
    return symbol, artifact_name


def _symbol_list(symbol_dir: Path) -> list[str]:
    if not symbol_dir.exists():
        return []
    return sorted(path.stem for path in symbol_dir.glob("*.parquet"))


def _inject_styles() -> None:
    st.markdown(
        """
        <style>
            .stApp {
                font-family: "Avenir Next", "Helvetica Neue", sans-serif;
            }
            .hero {
                background: radial-gradient(circle at top left, rgba(128, 128, 128, 0.08), transparent 65%);
                border: 1px solid rgba(128, 128, 128, 0.4);
                padding: 28px 32px;
                border-radius: 18px;
                margin-bottom: 18px;
            }
            .hero h1 {
                margin: 0;
                font-size: 2.1rem;
                line-height: 1.1;
                letter-spacing: -0.03em;
            }
            .hero p {
                margin: 8px 0 0;
                color: inherit;
                max-width: 960px;
                font-size: 1rem;
            }
            .eyebrow {
                color: inherit;
                font-size: 0.8rem;
                letter-spacing: 0.18em;
                text-transform: uppercase;
                margin-bottom: 10px;
                font-weight: 700;
            }
            [data-testid="stMetric"] {
                background: transparent;
                border: 1px solid rgba(128, 128, 128, 0.4);
                border-radius: 14px;
                padding: 10px 14px;
            }
            [data-testid="stCaptionContainer"] {
                font-size: 0.95rem;
                opacity: 1;
            }
            [data-testid="stMarkdownContainer"] p,
            [data-testid="stMarkdownContainer"] li {
                line-height: 1.6;
            }
            [data-baseweb="tab"] p {
                font-size: 1rem;
                font-weight: 600;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _line_chart(x, y, name: str, color: str, height: int) -> go.Figure:
    figure = go.Figure(
        data=[
            go.Scatter(
                x=x,
                y=y,
                mode="lines",
                name=name,
                line={"color": color, "width": 2.5},
            )
        ]
    )
    figure.update_layout(
        margin={"l": 10, "r": 10, "t": 10, "b": 10},
        height=height,
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
    )
    return figure


def _bar_chart(frame: pd.DataFrame, x: str, y: str, color: str, y_title: str, height: int) -> go.Figure:
    figure = go.Figure(
        data=[
            go.Bar(
                x=frame[x].astype(str),
                y=frame[y],
                marker={"color": color},
            )
        ]
    )
    figure.update_layout(
        height=height,
        margin={"l": 10, "r": 10, "t": 10, "b": 10},
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        xaxis_title=x,
        yaxis_title=y_title,
    )
    return figure


def _grouped_bar_chart(frame: pd.DataFrame, x: str, y: str, group: str, y_title: str, height: int) -> go.Figure:
    figure = go.Figure()
    for group_value, group_frame in frame.groupby(group):
        figure.add_trace(
            go.Bar(
                x=group_frame[x].astype(str),
                y=group_frame[y],
                name=str(group_value),
            )
        )
    figure.update_layout(
        barmode="group",
        height=height,
        margin={"l": 10, "r": 10, "t": 10, "b": 10},
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        xaxis_title=x,
        yaxis_title=y_title,
    )
    return figure


def _heatmap(frame: pd.DataFrame, height: int) -> go.Figure:
    figure = go.Figure(
        data=[
            go.Heatmap(
                z=frame.values,
                x=[str(value) for value in frame.columns],
                y=[str(value) for value in frame.index],
                colorscale="Blues",
            )
        ]
    )
    figure.update_layout(
        height=height,
        margin={"l": 10, "r": 10, "t": 10, "b": 10},
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
    )
    return figure


def _read_csv(path: Path, **kwargs) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, **kwargs)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def _read_parquet(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    frame = pd.read_parquet(path)
    frame.index = pd.to_datetime(frame.index)
    return frame


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _fmt_pct(value: float) -> str:
    return f"{value:.2%}"


def _fmt_num(value: float) -> str:
    return f"{value:.2f}"
