from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from trader_engine.analytics.diagnostics import (
    performance_by_exit_reason,
    performance_by_holding_period_bucket,
    performance_by_asset_class,
    performance_by_state_family,
    performance_by_state_model,
    performance_by_volatility_regime,
    state_edge_summary,
    payoff_asymmetry_summary,
    trade_diagnostics,
    transition_edge_summary,
)
from trader_engine.research.context import PreparedDataset, ResearchContext
from trader_engine.research.quality import build_quality_gate_tables, quality_report_frames
from trader_engine.research.selection import apply_opportunity_caps, selection_report_frames


@dataclass
class FoldWindow:
    fold_id: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    validation_start: pd.Timestamp | None
    validation_end: pd.Timestamp | None
    test_start: pd.Timestamp
    test_end: pd.Timestamp

    def to_record(self) -> dict[str, object]:
        return {
            "fold_id": self.fold_id,
            "train_start": self.train_start,
            "train_end": self.train_end,
            "validation_start": self.validation_start,
            "validation_end": self.validation_end,
            "test_start": self.test_start,
            "test_end": self.test_end,
        }


@dataclass
class WalkForwardResult:
    folds: pd.DataFrame
    fold_metrics: pd.DataFrame
    aggregate_metrics: pd.DataFrame
    trades: pd.DataFrame
    orders: pd.DataFrame
    attribution_frames: dict[str, pd.DataFrame]
    diagnostic_frames: dict[str, pd.DataFrame]
    quality_frames: dict[str, pd.DataFrame]


@dataclass
class TestSignalInput:
    member: object
    frame: pd.DataFrame
    analysis: object


@dataclass
class FoldSignalPlan:
    fold: FoldWindow
    validation_frames: dict[str, pd.DataFrame]
    validation_members: dict[str, object]
    test_inputs: dict[str, TestSignalInput]


@dataclass
class PreparedWalkForwardRun:
    folds: list[FoldSignalPlan]
    folds_frame: pd.DataFrame
    risk_history_by_symbol: dict[str, pd.Series] = field(default_factory=dict)


class WalkForwardRunner:
    def __init__(self, context: ResearchContext) -> None:
        self.context = context
        self.config = context.config
        self.walk_forward_config = context.config.research.walk_forward

    def run(self, dataset: PreparedDataset) -> WalkForwardResult:
        prepared = self.prepare(dataset)
        return self.run_prepared(prepared)

    def prepare(self, dataset: PreparedDataset) -> PreparedWalkForwardRun:
        folds = self._build_folds(dataset.feature_by_symbol)
        if not folds:
            return PreparedWalkForwardRun(folds=[], folds_frame=pd.DataFrame())

        fold_plans: list[FoldSignalPlan] = []

        for fold in folds:
            validation_frames: dict[str, pd.DataFrame] = {}
            validation_members: dict[str, object] = {}
            test_inputs: dict[str, TestSignalInput] = {}

            for symbol, feature_frame in dataset.feature_by_symbol.items():
                member = dataset.members_by_symbol[symbol]
                bundle = self.context.bundle_for(member.asset_class)
                train_features = self._slice(feature_frame, fold.train_start, fold.train_end)
                validation_features = self._slice(feature_frame, fold.validation_start, fold.validation_end)
                test_features = self._slice(feature_frame, fold.test_start, fold.test_end)

                if len(train_features) < self.walk_forward_config.minimum_symbol_train_bars:
                    continue

                train_state_model = bundle.create_state_model()
                train_classified = train_state_model.fit_transform(train_features)
                train_analysis = bundle.markov_analyzer.analyze(train_classified)
                if not validation_features.empty:
                    validation_context = pd.concat([train_features, validation_features]).sort_index()
                    validation_classified = train_state_model.classify(validation_context)
                    validation_frame = validation_classified.loc[validation_features.index]
                    validation_frames[symbol] = bundle.signal_engine.generate_signals_from_analysis(
                        member=member,
                        frame=validation_frame,
                        analysis=train_analysis,
                    ).assign(
                        asset_class=member.asset_class.value,
                        model_name=bundle.resolved_config.states.model_name,
                        selection_policy_name=bundle.resolved_config.signals.selection_policy_name,
                    )
                    validation_members[symbol] = member

                test_analysis = train_analysis
                if not test_features.empty:
                    if self.walk_forward_config.refit_on_validation_for_test and not validation_features.empty:
                        test_state_model = bundle.create_state_model()
                        test_training_features = pd.concat([train_features, validation_features]).sort_index()
                        test_training_classified = test_state_model.fit_transform(test_training_features)
                        test_analysis = bundle.markov_analyzer.analyze(test_training_classified)
                        test_context = pd.concat([test_training_features, test_features]).sort_index()
                        test_classified_full = test_state_model.classify(test_context)
                    else:
                        test_context_parts = [train_features]
                        if not validation_features.empty:
                            test_context_parts.append(validation_features)
                        test_context_parts.append(test_features)
                        test_context = pd.concat(test_context_parts).sort_index()
                        test_classified_full = train_state_model.classify(test_context)
                    test_frame = test_classified_full.loc[test_features.index]
                    test_inputs[symbol] = TestSignalInput(member=member, frame=test_frame, analysis=test_analysis)

            fold_plans.append(
                FoldSignalPlan(
                    fold=fold,
                    validation_frames=validation_frames,
                    validation_members=validation_members,
                    test_inputs=test_inputs,
                )
            )

        return PreparedWalkForwardRun(
            folds=fold_plans,
            folds_frame=pd.DataFrame([fold.to_record() for fold in folds]),
            risk_history_by_symbol={s: f['close'] for s, f in dataset.raw_by_symbol.items()},
        )

    def run_prepared(
        self,
        prepared: PreparedWalkForwardRun,
        backtest_engine=None,
    ) -> WalkForwardResult:
        if not prepared.folds:
            empty = pd.DataFrame()
            return WalkForwardResult(empty, empty, empty, empty, empty, {}, {}, {})

        engine = backtest_engine or self.context.build_backtest_engine()
        fold_metrics_records: list[dict[str, object]] = []
        trade_frames: list[pd.DataFrame] = []
        order_frames: list[pd.DataFrame] = []
        validation_trade_history: list[pd.DataFrame] = []
        candidate_frames: list[pd.DataFrame] = []

        for fold_plan in prepared.folds:
            fold = fold_plan.fold
            validation_frames = fold_plan.validation_frames
            validation_members = fold_plan.validation_members

            if validation_frames:
                validation_caps = apply_opportunity_caps(validation_frames)
                if not validation_caps.candidates.empty:
                    candidate_frames.append(validation_caps.candidates.assign(fold_id=fold.fold_id, split="validation"))
                validation_result = engine.run(
                    frames_by_symbol=validation_caps.frames_by_symbol,
                    members_by_symbol=validation_members,
                    initial_capital=self.config.risk.initial_capital,
                    risk_history_by_symbol=prepared.risk_history_by_symbol or None,
                )
                fold_metrics_records.append(
                    self._metric_record(fold, "validation", validation_result.metrics, len(validation_frames))
                )
                if not validation_result.trades.empty:
                    validation_trades = validation_result.trades.assign(fold_id=fold.fold_id, split="validation")
                    trade_frames.append(validation_trades)
                    validation_trade_history.append(validation_trades)
                if not validation_result.orders.empty:
                    order_frames.append(validation_result.orders.assign(fold_id=fold.fold_id, split="validation"))

            quality_tables, _ = build_quality_gate_tables(
                pd.concat(validation_trade_history, ignore_index=True) if validation_trade_history else pd.DataFrame(),
                min_group_observations=self.config.research.diagnostics.min_group_observations,
            )
            test_frames: dict[str, pd.DataFrame] = {}
            test_members: dict[str, object] = {}
            for symbol, test_input in fold_plan.test_inputs.items():
                bundle = self.context.bundle_for(test_input.member.asset_class)
                test_frames[symbol] = bundle.signal_engine.generate_signals_from_analysis(
                    member=test_input.member,
                    frame=test_input.frame,
                    analysis=test_input.analysis,
                    quality_tables=quality_tables,
                ).assign(
                    asset_class=test_input.member.asset_class.value,
                    model_name=bundle.resolved_config.states.model_name,
                    selection_policy_name=bundle.resolved_config.signals.selection_policy_name,
                )
                test_members[symbol] = test_input.member

            if test_frames:
                test_caps = apply_opportunity_caps(test_frames)
                if not test_caps.candidates.empty:
                    candidate_frames.append(test_caps.candidates.assign(fold_id=fold.fold_id, split="test"))
                test_result = engine.run(
                    frames_by_symbol=test_caps.frames_by_symbol,
                    members_by_symbol=test_members,
                    initial_capital=self.config.risk.initial_capital,
                    risk_history_by_symbol=prepared.risk_history_by_symbol or None,
                )
                fold_metrics_records.append(self._metric_record(fold, "test", test_result.metrics, len(test_frames)))
                if not test_result.trades.empty:
                    trade_frames.append(test_result.trades.assign(fold_id=fold.fold_id, split="test"))
                if not test_result.orders.empty:
                    order_frames.append(test_result.orders.assign(fold_id=fold.fold_id, split="test"))

        fold_metrics = pd.DataFrame(fold_metrics_records)
        aggregate_metrics = self._aggregate_metrics(fold_metrics)
        trades = pd.concat(trade_frames, ignore_index=True) if trade_frames else pd.DataFrame()
        orders = pd.concat(order_frames, ignore_index=True) if order_frames else pd.DataFrame()
        candidates = pd.concat(candidate_frames, ignore_index=True) if candidate_frames else pd.DataFrame()
        attribution_frames = self._attribution_frames(trades)
        diagnostic_frames = self._diagnostic_frames(trades)
        quality_frames = self._quality_frames(trades, candidates)
        folds_frame = prepared.folds_frame
        return WalkForwardResult(
            folds=folds_frame,
            fold_metrics=fold_metrics,
            aggregate_metrics=aggregate_metrics,
            trades=trades,
            orders=orders,
            attribution_frames=attribution_frames,
            diagnostic_frames=diagnostic_frames,
            quality_frames=quality_frames,
        )

    def _build_folds(self, frames_by_symbol: dict[str, pd.DataFrame]) -> list[FoldWindow]:
        timeline = sorted({timestamp for frame in frames_by_symbol.values() for timestamp in frame.index})
        if not timeline:
            return []

        validation_bars = max(self.walk_forward_config.validation_bars, 0)
        total_window = (
            self.walk_forward_config.train_bars + validation_bars + self.walk_forward_config.test_bars
        )
        if len(timeline) < total_window:
            return []

        folds: list[FoldWindow] = []
        cursor = 0
        fold_id = 1
        while cursor + total_window <= len(timeline):
            train_start = timeline[cursor]
            train_end = timeline[cursor + self.walk_forward_config.train_bars - 1]
            validation_start = None
            validation_end = None
            if validation_bars > 0:
                validation_start = timeline[cursor + self.walk_forward_config.train_bars]
                validation_end = timeline[cursor + self.walk_forward_config.train_bars + validation_bars - 1]
                test_start_index = cursor + self.walk_forward_config.train_bars + validation_bars
            else:
                test_start_index = cursor + self.walk_forward_config.train_bars
            test_start = timeline[test_start_index]
            test_end = timeline[cursor + total_window - 1]
            folds.append(
                FoldWindow(
                    fold_id=fold_id,
                    train_start=train_start,
                    train_end=train_end,
                    validation_start=validation_start,
                    validation_end=validation_end,
                    test_start=test_start,
                    test_end=test_end,
                )
            )
            cursor += self.walk_forward_config.step_bars
            fold_id += 1
        return folds

    @staticmethod
    def _slice(frame: pd.DataFrame, start: pd.Timestamp | None, end: pd.Timestamp | None) -> pd.DataFrame:
        if start is None or end is None:
            return pd.DataFrame(columns=frame.columns)
        return frame.loc[(frame.index >= start) & (frame.index <= end)].copy()

    @staticmethod
    def _metric_record(
        fold: FoldWindow,
        split: str,
        metrics: dict[str, float | int],
        symbol_count: int,
    ) -> dict[str, object]:
        record = {"fold_id": fold.fold_id, "split": split, "symbol_count": symbol_count}
        record.update(metrics)
        return record

    @staticmethod
    def _aggregate_metrics(fold_metrics: pd.DataFrame) -> pd.DataFrame:
        if fold_metrics.empty:
            return pd.DataFrame()
        return (
            fold_metrics.groupby("split")
            .agg(
                fold_count=("fold_id", "nunique"),
                mean_total_return=("total_return", "mean"),
                mean_sharpe=("sharpe", "mean"),
                mean_max_drawdown=("max_drawdown", "mean"),
                mean_trade_count=("trade_count", "mean"),
                mean_expectancy=("expectancy", "mean"),
                mean_average_win=("average_win", "mean"),
                mean_average_loss=("average_loss", "mean"),
                mean_win_rate=("win_rate", "mean"),
                positive_fold_rate=("total_return", lambda series: (series > 0).mean()),
            )
            .reset_index()
        )

    def _attribution_frames(self, trades: pd.DataFrame) -> dict[str, pd.DataFrame]:
        diagnostics_config = self.config.research.diagnostics
        frames: dict[str, pd.DataFrame] = {}
        for split, split_trades in self._split_map(trades).items():
            frames[f"{split}_state_edge_summary"] = state_edge_summary(
                split_trades,
                min_group_observations=diagnostics_config.min_group_observations,
            )
            frames[f"{split}_transition_edge_summary"] = transition_edge_summary(
                split_trades,
                min_group_observations=diagnostics_config.min_group_observations,
            )
            frames[f"{split}_volatility_regime_performance"] = performance_by_volatility_regime(
                split_trades,
                min_group_observations=diagnostics_config.min_group_observations,
            )
            frames[f"{split}_asset_class_performance"] = performance_by_asset_class(
                split_trades,
                min_group_observations=diagnostics_config.min_group_observations,
            )
            frames[f"{split}_exit_reason_performance"] = performance_by_exit_reason(
                split_trades,
                min_group_observations=diagnostics_config.min_group_observations,
            )
            frames[f"{split}_state_family_performance"] = performance_by_state_family(
                split_trades,
                min_group_observations=diagnostics_config.min_group_observations,
            )
            frames[f"{split}_state_model_performance"] = performance_by_state_model(
                split_trades,
                min_group_observations=diagnostics_config.min_group_observations,
            )
            frames[f"{split}_holding_period_performance"] = performance_by_holding_period_bucket(
                split_trades,
                min_group_observations=diagnostics_config.min_group_observations,
            )
        return frames

    def _diagnostic_frames(self, trades: pd.DataFrame) -> dict[str, pd.DataFrame]:
        top_n = self.config.research.diagnostics.top_n_trades
        frames: dict[str, pd.DataFrame] = {}
        for split, split_trades in self._split_map(trades).items():
            frames[f"{split}_payoff_asymmetry_summary"] = payoff_asymmetry_summary(split_trades)
            diagnostics = trade_diagnostics(split_trades, top_n=top_n)
            for key, frame in diagnostics.items():
                frames[f"{split}_{key}"] = frame
        return frames

    def _quality_frames(self, trades: pd.DataFrame, candidates: pd.DataFrame) -> dict[str, pd.DataFrame]:
        diagnostics_config = self.config.research.diagnostics
        frames: dict[str, pd.DataFrame] = {}
        for split, split_trades in self._split_map(trades).items():
            reports = quality_report_frames(
                split_trades,
                min_group_observations=diagnostics_config.min_group_observations,
                top_n=diagnostics_config.top_n_trades,
            )
            for key, frame in reports.items():
                frames[f"{split}_{key}"] = frame
        for split, split_candidates in self._split_map(candidates).items():
            reports = selection_report_frames(split_candidates)
            for key, frame in reports.items():
                frames[f"{split}_{key}"] = frame
        return frames

    @staticmethod
    def _split_map(frame: pd.DataFrame) -> dict[str, pd.DataFrame]:
        if frame.empty or "split" not in frame:
            return {"all": frame}
        split_map = {split: chunk.copy() for split, chunk in frame.groupby("split")}
        split_map["all"] = frame.copy()
        return split_map


@dataclass(frozen=True)
class SessionFold:
    fold_id: int
    train_sessions: tuple
    validation_sessions: tuple
    test_sessions: tuple
    separation_sessions: int = 5


def build_session_folds(sessions, train=252, validation=63, test=63, step=63, separation=5):
    """Chronological exchange-session folds with two explicit embargo boundaries.

    All test sets are disjoint. No daily/calendar resampling creates phantom sessions.
    The last incomplete fold is omitted, never shortened after viewing outcomes.
    """
    dates=tuple(pd.Timestamp(x).date() for x in sessions)
    if dates != tuple(sorted(set(dates))):raise ValueError('Sessions must be unique and chronological')
    if min(train,validation,test,step)<=0 or separation<5 or step<test:
        raise ValueError('Positive windows, >=5-session separation and nonoverlapping tests required')
    folds=[];width=train+validation+test+2*separation
    for start in range(0,len(dates)-width+1,step):
        v=start+train+separation;t=v+validation+separation
        folds.append(SessionFold(len(folds)+1,dates[start:start+train],dates[v:v+validation],dates[t:t+test],separation))
    return folds


def purge_overlapping_labels(labels, split_sessions, next_split_start):
    """Exclude realized labels crossing a split boundary before any fitting/scoring."""
    if not {'signal_session','label_end_session'}.issubset(labels):
        raise ValueError('Explicit signal and label-end sessions required for purging')
    allowed={pd.Timestamp(x).date() for x in split_sessions};boundary=pd.Timestamp(next_split_start).date()
    signal=pd.to_datetime(labels.signal_session).dt.date
    end=pd.to_datetime(labels.label_end_session).dt.date
    return labels.loc[signal.isin(allowed)&end.notna()&(end<boundary)].copy()


def run_etf_continuous(frames_by_symbol,spec,*,schedule,folds=None,**kwargs):
    """One account across all test folds, including intervening cash/carry sessions.

    Fixed candidate decisions have no fitted parameters. Selection/calibration belongs
    to the caller and may only consume the returned fold's training/validation ranges.
    This API does not claim that strategy selection or confirmation has occurred.
    """
    from trader_engine.backtest.engine import BacktestEngine
    dates=pd.to_datetime(schedule.market_open,utc=True).dt.tz_convert('America/New_York').dt.date
    folds=build_session_folds(dates) if folds is None else folds
    if not folds:raise ValueError('Insufficient sessions for a complete walk-forward fold')
    tests=[d for f in folds for d in f.test_sessions]
    if len(tests)!=len(set(tests)):raise ValueError('Overlapping test sessions')
    active=schedule.loc[(dates>=min(tests))&(dates<=max(tests))]
    result=BacktestEngine.run_etf_replay(frames_by_symbol,spec,schedule=active,entry_sessions=tests,**kwargs)
    result.metrics['test_folds']=len(folds)
    result.metrics['capital_initializations']=1
    return result


WalkForwardRunner.build_session_folds = staticmethod(build_session_folds)
WalkForwardRunner.run_etf_continuous = staticmethod(run_etf_continuous)
