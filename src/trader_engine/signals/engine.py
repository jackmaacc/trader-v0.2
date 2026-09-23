from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from trader_engine.core.config import FeatureConfig, MarkovConfig, SignalConfig
from trader_engine.core.models import AssetClass, Opportunity, SignalDirection, UniverseMember
from trader_engine.markov.engine import MarkovAnalysisResult, MarkovAnalyzer


@dataclass
class QualityGateTables:
    state_quality: pd.DataFrame = field(default_factory=pd.DataFrame)
    transition_quality: pd.DataFrame = field(default_factory=pd.DataFrame)
    state_family_quality: pd.DataFrame = field(default_factory=pd.DataFrame)


@dataclass
class SignalSnapshot:
    direction: SignalDirection
    candidate_direction: SignalDirection
    current_state: str
    expected_return: float
    expected_value: float
    confidence: float
    score: float
    base_score: float
    signal_quality: float
    volatility: float
    liquidity_score: float
    transition_entropy: float
    sample_size: int
    predicted_next_state: str | None
    transition_accuracy: float
    state_family: str
    transition_setup: str | None
    opportunity_score: float
    state_quality_score: float
    transition_quality_score: float
    state_family_quality_score: float
    family_rank_score: float
    fold_consistency_score: float
    sample_size_score: float
    trade_count_score: float
    tradability_score: float
    selection_policy_name: str | None
    selection_mode: str
    state_gate_pass: bool
    transition_gate_pass: bool
    consistency_gate_pass: bool
    family_gate_pass: bool
    gate_passed: bool
    hard_filter_passed: bool
    hard_block_reason: str | None
    hard_block_category: str | None
    selection_passed: bool
    diagnostics: dict[str, object] = field(default_factory=dict)


class SignalEngine:
    def __init__(
        self,
        signal_config: SignalConfig,
        markov_config: MarkovConfig,
        feature_config: FeatureConfig,
    ) -> None:
        self.signal_config = signal_config
        self.markov_config = markov_config
        self.feature_config = feature_config
        self.realized_vol_column = f"realized_vol_{feature_config.volatility_window}"
        self.dollar_volume_column = f"dollar_volume_{feature_config.volume_window}"

    def evaluate_snapshot(
        self,
        member: UniverseMember,
        row: pd.Series,
        analysis: MarkovAnalysisResult,
        quality_tables: QualityGateTables | None = None,
    ) -> SignalSnapshot:
        current_state = str(row.get("state", "unknown"))
        state_family = str(row.get("state_family", "unknown"))
        stats = analysis.stats_for(current_state)
        if stats.empty:
            return self._flat_snapshot(current_state=current_state, state_family=state_family)

        predicted_distribution = analysis.distribution_for(current_state)
        predicted_next_state = predicted_distribution.index[0] if not predicted_distribution.empty else None
        transition_setup = (
            f"{current_state} -> {predicted_next_state}" if predicted_next_state not in {None, "", "None"} else None
        )

        expected_return = float(stats["mean_return"])
        observations = int(stats["observations"])
        transition_entropy = float(analysis.state_entropy.get(current_state, 1.0))
        confidence = self._confidence_for(current_state=current_state, analysis=analysis, observations=observations)
        volatility = self._safe_float(row.get(self.realized_vol_column), default=0.0)
        avg_dollar_volume = self._safe_float(row.get(self.dollar_volume_column), default=0.0)
        liquidity_score = min(1.0, avg_dollar_volume / max(self.signal_config.liquidity_threshold, 1.0))

        cost_penalty = (self.signal_config.transaction_cost_bps + self.signal_config.slippage_bps) / 10_000.0
        volatility_penalty = self.signal_config.volatility_penalty * max(volatility, 0.0)
        liquidity_penalty = 0.0
        if avg_dollar_volume < self.signal_config.liquidity_threshold:
            liquidity_penalty = self.signal_config.liquidity_penalty * (
                self.signal_config.liquidity_threshold / max(avg_dollar_volume, 1.0) - 1.0
            )

        long_expected_value = expected_return - cost_penalty - volatility_penalty - liquidity_penalty
        short_expected_value = -expected_return - cost_penalty - volatility_penalty - liquidity_penalty

        tables = quality_tables or QualityGateTables()
        state_quality_available = not tables.state_quality.empty
        transition_quality_available = not tables.transition_quality.empty
        family_quality_available = not tables.state_family_quality.empty
        state_quality = self._lookup_quality(
            table=tables.state_quality,
            key_column="state",
            key=current_state,
            asset_class=member.asset_class,
        )
        transition_quality = self._lookup_quality(
            table=tables.transition_quality,
            key_column="transition_setup",
            key=transition_setup,
            asset_class=member.asset_class,
        )
        family_quality = self._lookup_quality(
            table=tables.state_family_quality,
            key_column="state_family",
            key=state_family,
            asset_class=member.asset_class,
        )

        state_quality_score = float(state_quality.get("quality_score", 0.0))
        transition_quality_score = float(transition_quality.get("quality_score", 0.0))
        state_family_quality_score = float(family_quality.get("quality_score", 0.0))
        family_rank_score = self._record_rank_score(family_quality, prefix="quality")
        fold_consistency_score = self._setup_consistency_score(state_quality, transition_quality)
        sample_size_score = self._setup_sample_size_score(state_quality, transition_quality, observations)
        trade_count_score = self._setup_trade_count_score(state_quality, transition_quality, observations)
        tradability_score = self._weighted_average(
            {
                "state_quality": state_quality_score,
                "transition_quality": transition_quality_score,
                "fold_consistency": fold_consistency_score,
                "sample_size": sample_size_score,
                "state_family_quality": state_family_quality_score,
            }
        )

        state_gate_pass, state_checks = self._evaluate_quality_gate(
            state_quality,
            kind="state",
            table_available=state_quality_available,
        )
        transition_gate_pass, transition_checks = self._evaluate_quality_gate(
            transition_quality,
            kind="transition",
            table_available=transition_quality_available,
        )
        sample_size_gate_pass, sample_size_checks = self._evaluate_sample_size_gate(
            state_quality=state_quality,
            transition_quality=transition_quality,
            sample_size_score=sample_size_score,
            observations=observations,
            quality_available=state_quality_available or transition_quality_available,
        )
        consistency_gate_pass, consistency_checks = self._evaluate_consistency_gate(
            state_quality=state_quality,
            transition_quality=transition_quality,
            quality_available=state_quality_available or transition_quality_available,
        )
        family_gate_pass, family_mode_pass, family_checks = self._evaluate_family_gate(
            family_quality=family_quality,
            state_family=state_family,
            table_available=family_quality_available,
        )
        family_list_pass = bool(family_checks["family_allowed_pass"] and family_checks["family_excluded_pass"])

        candidate_direction, candidate_expected_value, base_score, candidate_checks = self._candidate_decision(
            member=member,
            long_expected_value=long_expected_value,
            short_expected_value=short_expected_value,
            confidence=confidence,
            observations=observations,
            volatility=volatility,
            liquidity_score=liquidity_score,
        )
        signal_quality = abs(expected_return) * confidence * max(liquidity_score, 0.05)
        opportunity_score = self._bounded_score(base_score)
        selection_score, score_contributions, score_components = self._selection_score(
            base_score=base_score,
            opportunity_score=opportunity_score,
            state_quality_score=state_quality_score,
            transition_quality_score=transition_quality_score,
            family_rank_score=family_rank_score,
            fold_consistency_score=fold_consistency_score,
            sample_size_score=sample_size_score,
            trade_count_score=trade_count_score,
            tradability_score=tradability_score,
        )
        if self.signal_config.selection_mode == "hard_gate_only":
            selection_score = base_score

        gate_passed = family_gate_pass and self._component_gate_pass(
            state_gate_pass=state_gate_pass,
            transition_gate_pass=transition_gate_pass,
            consistency_gate_pass=consistency_gate_pass,
        )
        hard_filter_passed, hard_block_category, hard_block_reason = self._evaluate_hard_filters(
            candidate_direction=candidate_direction,
            candidate_checks=candidate_checks,
            family_list_pass=family_list_pass,
            family_mode_pass=family_mode_pass,
            state_gate_pass=state_gate_pass,
            transition_gate_pass=transition_gate_pass,
            consistency_gate_pass=consistency_gate_pass,
            sample_size_gate_pass=sample_size_gate_pass,
            legacy_gate_pass=gate_passed,
        )
        direction = candidate_direction if hard_filter_passed else SignalDirection.FLAT
        expected_value = candidate_expected_value if direction != SignalDirection.FLAT else 0.0
        selection_passed = direction != SignalDirection.FLAT

        diagnostics = {
            "base_signal_score": base_score,
            "opportunity_score": opportunity_score,
            "candidate_signal": candidate_direction.value,
            "selection_policy_name": self.signal_config.selection_policy_name,
            "selection_mode": self.signal_config.selection_mode,
            "active_gate_components": ",".join(self._gate_components()),
            "active_hard_filters": ",".join(self._hard_filter_components()),
            "selection_cap_overall_limit": self.signal_config.top_n_per_rebalance_date,
            "selection_cap_asset_class_limit": self.signal_config.top_n_per_asset_class,
            "selection_cap_model_limit": self.signal_config.top_n_per_model,
            "state_family_quality_score": state_family_quality_score,
            "family_rank_score": family_rank_score,
            "sample_size_score": sample_size_score,
            "trade_count_score": trade_count_score,
            "tradability_score": tradability_score,
            "selection_passed": selection_passed,
            "hard_filter_passed": hard_filter_passed,
            "hard_block_category": hard_block_category,
            "hard_block_reason": hard_block_reason,
            "consistency_gate_pass": consistency_gate_pass,
            "state_quality_rank": self._safe_int(state_quality.get("quality_rank")),
            "transition_quality_rank": self._safe_int(transition_quality.get("quality_rank")),
            "state_quality_percentile": self._safe_float(state_quality.get("quality_percentile")),
            "transition_quality_percentile": self._safe_float(transition_quality.get("quality_percentile")),
            "state_consistency_rank": self._safe_int(state_quality.get("consistency_rank")),
            "transition_consistency_rank": self._safe_int(transition_quality.get("consistency_rank")),
            "state_consistency_percentile": self._safe_float(state_quality.get("consistency_percentile")),
            "transition_consistency_percentile": self._safe_float(transition_quality.get("consistency_percentile")),
            "state_family_quality_rank": self._safe_int(family_quality.get("quality_rank")),
            "state_family_quality_percentile": self._safe_float(family_quality.get("quality_percentile")),
            "state_family_consistency_score": self._safe_float(family_quality.get("consistency_score")),
            "state_family_consistency_rank": self._safe_int(family_quality.get("consistency_rank")),
            "state_family_consistency_percentile": self._safe_float(family_quality.get("consistency_percentile")),
            **state_checks,
            **transition_checks,
            **sample_size_checks,
            **consistency_checks,
            **family_checks,
        }
        for component_name, value in score_components.items():
            diagnostics[f"score_component_{component_name}"] = value
            diagnostics[f"score_contribution_{component_name}"] = score_contributions.get(component_name, 0.0)

        return SignalSnapshot(
            direction=direction,
            candidate_direction=candidate_direction,
            current_state=current_state,
            expected_return=expected_return,
            expected_value=expected_value,
            confidence=confidence,
            score=selection_score,
            base_score=base_score,
            signal_quality=signal_quality,
            volatility=volatility,
            liquidity_score=liquidity_score,
            transition_entropy=transition_entropy,
            sample_size=observations,
            predicted_next_state=predicted_next_state,
            transition_accuracy=analysis.accuracy,
            state_family=state_family,
            transition_setup=transition_setup,
            opportunity_score=opportunity_score,
            state_quality_score=state_quality_score,
            transition_quality_score=transition_quality_score,
            state_family_quality_score=state_family_quality_score,
            family_rank_score=family_rank_score,
            fold_consistency_score=fold_consistency_score,
            sample_size_score=sample_size_score,
            trade_count_score=trade_count_score,
            tradability_score=tradability_score,
            selection_policy_name=self.signal_config.selection_policy_name,
            selection_mode=self.signal_config.selection_mode,
            state_gate_pass=state_gate_pass,
            transition_gate_pass=transition_gate_pass,
            consistency_gate_pass=consistency_gate_pass,
            family_gate_pass=family_gate_pass,
            gate_passed=gate_passed,
            hard_filter_passed=hard_filter_passed,
            hard_block_reason=hard_block_reason,
            hard_block_category=hard_block_category,
            selection_passed=selection_passed,
            diagnostics=diagnostics,
        )

    def generate_historical_signals(
        self,
        member: UniverseMember,
        frame: pd.DataFrame,
        analyzer: MarkovAnalyzer,
        quality_tables: QualityGateTables | None = None,
    ) -> pd.DataFrame:
        signal_frame = self._initialize_signal_columns(frame)

        # The analyzer excludes unrealized training labels; inference requires
        # no future labels and must include the latest completed bar.
        upper_bound = len(signal_frame)
        for row_number in range(self.signal_config.min_training_bars, upper_bound):
            training = signal_frame.iloc[: row_number + 1].copy()
            analysis = analyzer.analyze(training)
            snapshot = self.evaluate_snapshot(
                member=member,
                row=training.iloc[-1],
                analysis=analysis,
                quality_tables=quality_tables,
            )
            index = signal_frame.index[row_number]
            self._write_snapshot(signal_frame, index, snapshot)

        return signal_frame

    def generate_signals_from_analysis(
        self,
        member: UniverseMember,
        frame: pd.DataFrame,
        analysis: MarkovAnalysisResult,
        quality_tables: QualityGateTables | None = None,
    ) -> pd.DataFrame:
        signal_frame = self._initialize_signal_columns(frame)
        for index, row in signal_frame.iterrows():
            snapshot = self.evaluate_snapshot(
                member=member,
                row=row,
                analysis=analysis,
                quality_tables=quality_tables,
            )
            self._write_snapshot(signal_frame, index, snapshot)
        return signal_frame

    def opportunity_from_latest(
        self,
        member: UniverseMember,
        frame: pd.DataFrame,
        analysis: MarkovAnalysisResult,
        quality_tables: QualityGateTables | None = None,
    ) -> Opportunity:
        latest = frame.dropna(subset=["state"]).iloc[-1]
        snapshot = self.evaluate_snapshot(
            member=member,
            row=latest,
            analysis=analysis,
            quality_tables=quality_tables,
        )
        return Opportunity(
            symbol=member.symbol,
            asset_class=member.asset_class,
            as_of=latest.name.to_pydatetime(),
            current_state=snapshot.current_state,
            direction=snapshot.direction,
            expected_return=snapshot.expected_return,
            expected_value=snapshot.expected_value,
            confidence=snapshot.confidence,
            score=snapshot.score,
            signal_quality=snapshot.signal_quality,
            volatility=snapshot.volatility,
            liquidity_score=snapshot.liquidity_score,
            transition_entropy=snapshot.transition_entropy,
            sample_size=snapshot.sample_size,
            metadata={
                "predicted_next_state": snapshot.predicted_next_state,
                "transition_accuracy": snapshot.transition_accuracy,
                "state_family": snapshot.state_family,
                "transition_setup": snapshot.transition_setup,
                "opportunity_score": snapshot.opportunity_score,
                "state_quality_score": snapshot.state_quality_score,
                "transition_quality_score": snapshot.transition_quality_score,
                "state_family_quality_score": snapshot.state_family_quality_score,
                "family_rank_score": snapshot.family_rank_score,
                "fold_consistency_score": snapshot.fold_consistency_score,
                "sample_size_score": snapshot.sample_size_score,
                "trade_count_score": snapshot.trade_count_score,
                "tradability_score": snapshot.tradability_score,
                "selection_policy_name": snapshot.selection_policy_name,
                "selection_mode": snapshot.selection_mode,
                "state_gate_pass": snapshot.state_gate_pass,
                "transition_gate_pass": snapshot.transition_gate_pass,
                "consistency_gate_pass": snapshot.consistency_gate_pass,
                "family_gate_pass": snapshot.family_gate_pass,
                "gate_passed": snapshot.gate_passed,
                "hard_filter_passed": snapshot.hard_filter_passed,
                "hard_block_reason": snapshot.hard_block_reason,
                "hard_block_category": snapshot.hard_block_category,
                "selection_passed": snapshot.selection_passed,
                "candidate_signal": snapshot.candidate_direction.value,
                **snapshot.diagnostics,
            },
        )

    @staticmethod
    def opportunities_to_frame(opportunities: list[Opportunity]) -> pd.DataFrame:
        if not opportunities:
            return pd.DataFrame()
        records = [opportunity.to_record() for opportunity in opportunities]
        frame = pd.DataFrame(records)
        metadata = frame.pop("metadata").apply(pd.Series) if "metadata" in frame.columns else pd.DataFrame(index=frame.index)
        frame = pd.concat([frame, metadata], axis=1)
        return frame.sort_values(["score", "expected_value"], ascending=False).reset_index(drop=True)

    def _confidence_for(
        self,
        current_state: str,
        analysis: MarkovAnalysisResult,
        observations: int,
    ) -> float:
        state_confidence = float(analysis.state_confidence.get(current_state, 0.0))
        scale = np.log1p(observations) / np.log1p(max(self.markov_config.min_state_observations * 5, 2))
        return float(np.clip(state_confidence * scale, 0.0, 1.0))

    def _candidate_decision(
        self,
        member: UniverseMember,
        long_expected_value: float,
        short_expected_value: float,
        confidence: float,
        observations: int,
        volatility: float,
        liquidity_score: float,
    ) -> tuple[SignalDirection, float, float, dict[str, object]]:
        allow_short = (
            member.asset_class == AssetClass.CRYPTO and self.signal_config.allow_short_crypto
        ) or (
            member.asset_class == AssetClass.EQUITY and self.signal_config.allow_short_equities
        )
        confidence_pass = confidence >= self.signal_config.min_confidence
        observation_pass = observations >= self.markov_config.min_state_observations
        long_pass = (
            long_expected_value >= self.signal_config.min_expected_value
            and confidence_pass
            and observation_pass
        )
        short_pass = (
            allow_short
            and short_expected_value >= self.signal_config.min_expected_value
            and confidence_pass
            and observation_pass
        )

        direction = SignalDirection.FLAT
        expected_value = 0.0
        if long_pass:
            direction = SignalDirection.LONG
            expected_value = long_expected_value
        if short_pass and short_expected_value > expected_value:
            direction = SignalDirection.SHORT
            expected_value = short_expected_value

        base_score = 0.0
        if direction != SignalDirection.FLAT:
            base_score = expected_value * max(confidence, 0.0) * max(liquidity_score, 0.05) / max(volatility, 0.01)
        block_reason = None
        if direction == SignalDirection.FLAT:
            if not observation_pass:
                block_reason = "insufficient_observations"
            elif not confidence_pass:
                block_reason = "low_confidence"
            elif long_expected_value < self.signal_config.min_expected_value and (
                not allow_short or short_expected_value < self.signal_config.min_expected_value
            ):
                block_reason = "negative_expected_value"
            else:
                block_reason = "candidate_not_selected"
        return direction, expected_value, base_score, {
            "confidence_pass": confidence_pass,
            "observation_pass": observation_pass,
            "long_expected_value_pass": long_pass,
            "short_expected_value_pass": short_pass,
            "block_reason": block_reason,
        }

    def _selection_score(
        self,
        base_score: float,
        opportunity_score: float,
        state_quality_score: float,
        transition_quality_score: float,
        family_rank_score: float,
        fold_consistency_score: float,
        sample_size_score: float,
        trade_count_score: float,
        tradability_score: float,
    ) -> tuple[float, dict[str, float], dict[str, float]]:
        weights = self.signal_config.selection_score_weights
        components = {
            "base_signal": self._bounded_score(base_score),
            "opportunity_score": opportunity_score,
            "state_quality": state_quality_score,
            "transition_quality": transition_quality_score,
            "family_rank": family_rank_score,
            "fold_consistency": fold_consistency_score,
            "sample_size": sample_size_score,
            "trade_count": trade_count_score,
            "tradability": tradability_score,
        }
        active_weights = {
            name: float(weight)
            for name, weight in weights.items()
            if name in components and float(weight) > 0.0
        }
        if not active_weights:
            return 0.0, {name: 0.0 for name in components}, components
        denominator = sum(active_weights.values())
        numerator = sum(max(components[name], 0.0) * weight for name, weight in active_weights.items())
        contributions = {
            name: (max(components[name], 0.0) * weight) / denominator
            for name, weight in active_weights.items()
        }
        for name in components:
            contributions.setdefault(name, 0.0)
        return numerator / denominator, contributions, components

    def _evaluate_sample_size_gate(
        self,
        state_quality: dict[str, float],
        transition_quality: dict[str, float],
        sample_size_score: float,
        observations: int,
        quality_available: bool,
    ) -> tuple[bool, dict[str, bool]]:
        checks = {
            "sample_size_score_pass": sample_size_score >= self.signal_config.min_sample_size_score,
            "observation_count_pass": observations >= self.markov_config.min_state_observations,
        }
        if not quality_available:
            checks["state_trade_count_gate_pass"] = True
            checks["transition_trade_count_gate_pass"] = True
            return all(checks.values()), checks
        checks["state_trade_count_gate_pass"] = (
            int(state_quality.get("sample_size", 0)) >= self.signal_config.min_state_trade_count
        )
        checks["transition_trade_count_gate_pass"] = (
            int(transition_quality.get("sample_size", 0)) >= self.signal_config.min_transition_trade_count
        )
        return all(checks.values()), checks

    def _evaluate_quality_gate(
        self,
        record: dict[str, float],
        kind: str,
        table_available: bool,
    ) -> tuple[bool, dict[str, bool]]:
        if not table_available:
            prefix = "state" if kind == "state" else "transition"
            return True, {
                f"{prefix}_quality_score_pass": True,
                f"{prefix}_quality_percentile_pass": True,
                f"{prefix}_quality_rank_pass": True,
                f"{prefix}_trade_count_pass": True,
                f"{prefix}_active_folds_pass": True,
                f"{prefix}_positive_fold_fraction_pass": True,
                f"{prefix}_expectancy_variance_pass": True,
                f"{prefix}_sharpe_variance_pass": True,
            }
        quality_score = float(record.get("quality_score", 0.0))
        quality_percentile = float(record.get("quality_percentile", 0.0))
        quality_rank = self._safe_int(record.get("quality_rank"), default=1_000_000)
        sample_size = int(record.get("sample_size", 0))
        active_folds = int(record.get("active_fold_count", 0))
        positive_fold_fraction = float(record.get("positive_fold_fraction", 0.0))
        expectancy_variance = float(record.get("expectancy_variance", 0.0))
        sharpe_variance = float(record.get("sharpe_variance", 0.0))

        if kind == "state":
            checks = {
                "state_quality_score_pass": quality_score >= self.signal_config.min_state_quality_score,
                "state_quality_percentile_pass": quality_percentile >= self.signal_config.min_state_quality_percentile,
                "state_quality_rank_pass": (
                    True
                    if self.signal_config.max_state_quality_rank is None
                    else quality_rank <= self.signal_config.max_state_quality_rank
                ),
                "state_trade_count_pass": sample_size >= self.signal_config.min_state_trade_count,
                "state_active_folds_pass": active_folds >= self.signal_config.min_state_active_folds,
                "state_positive_fold_fraction_pass": (
                    positive_fold_fraction >= self.signal_config.min_state_positive_fold_fraction
                ),
                "state_expectancy_variance_pass": (
                    True
                    if self.signal_config.max_state_expectancy_variance is None
                    else expectancy_variance <= self.signal_config.max_state_expectancy_variance
                ),
                "state_sharpe_variance_pass": (
                    True
                    if self.signal_config.max_state_sharpe_variance is None
                    else sharpe_variance <= self.signal_config.max_state_sharpe_variance
                ),
            }
        else:
            checks = {
                "transition_quality_score_pass": (
                    quality_score >= self.signal_config.min_transition_quality_score
                ),
                "transition_quality_percentile_pass": (
                    quality_percentile >= self.signal_config.min_transition_quality_percentile
                ),
                "transition_quality_rank_pass": (
                    True
                    if self.signal_config.max_transition_quality_rank is None
                    else quality_rank <= self.signal_config.max_transition_quality_rank
                ),
                "transition_trade_count_pass": sample_size >= self.signal_config.min_transition_trade_count,
                "transition_active_folds_pass": active_folds >= self.signal_config.min_transition_active_folds,
                "transition_positive_fold_fraction_pass": (
                    positive_fold_fraction >= self.signal_config.min_transition_positive_fold_fraction
                ),
                "transition_expectancy_variance_pass": (
                    True
                    if self.signal_config.max_transition_expectancy_variance is None
                    else expectancy_variance <= self.signal_config.max_transition_expectancy_variance
                ),
                "transition_sharpe_variance_pass": (
                    True
                    if self.signal_config.max_transition_sharpe_variance is None
                    else sharpe_variance <= self.signal_config.max_transition_sharpe_variance
                ),
            }
        return all(checks.values()), checks

    def _evaluate_consistency_gate(
        self,
        state_quality: dict[str, float],
        transition_quality: dict[str, float],
        quality_available: bool,
    ) -> tuple[bool, dict[str, bool]]:
        if not quality_available:
            return True, {
                "consistency_score_pass": True,
                "consistency_percentile_pass": True,
                "consistency_rank_pass": True,
            }
        consistency_score = self._setup_consistency_score(state_quality, transition_quality)
        consistency_percentile = self._setup_consistency_percentile(state_quality, transition_quality)
        consistency_rank = self._setup_consistency_rank(state_quality, transition_quality)
        checks = {
            "consistency_score_pass": consistency_score >= self.signal_config.min_fold_consistency_score,
            "consistency_percentile_pass": consistency_percentile >= self.signal_config.min_consistency_percentile,
            "consistency_rank_pass": (
                True
                if self.signal_config.max_consistency_rank is None
                else consistency_rank <= self.signal_config.max_consistency_rank
            ),
        }
        return all(checks.values()), checks

    def _evaluate_family_gate(
        self,
        family_quality: dict[str, float],
        state_family: str,
        table_available: bool,
    ) -> tuple[bool, bool, dict[str, bool]]:
        allowed = (
            not self.signal_config.allowed_state_families or state_family in self.signal_config.allowed_state_families
        )
        excluded = state_family not in self.signal_config.excluded_state_families
        family_rank = self._safe_int(family_quality.get("quality_rank"), default=1_000_000)
        family_percentile = self._safe_float(family_quality.get("quality_percentile"))
        family_group_size = self._safe_int(family_quality.get("quality_group_size"), default=0)
        family_consistency_score = self._safe_float(family_quality.get("consistency_score"))
        top_count_pass = (
            True
            if self.signal_config.top_state_family_count is None or not table_available
            else family_rank <= self.signal_config.top_state_family_count
        )
        top_percentile_pass = (
            True
            if self.signal_config.top_state_family_percentile is None or not table_available
            else family_percentile >= self.signal_config.top_state_family_percentile
        )
        bottom_pass = True
        if (
            self.signal_config.exclude_bottom_state_family_count is not None
            and family_group_size > 0
            and table_available
        ):
            exclusion_threshold = max(family_group_size - self.signal_config.exclude_bottom_state_family_count + 1, 1)
            bottom_pass = family_rank < exclusion_threshold
        bottom_percentile_pass = (
            True
            if self.signal_config.exclude_bottom_state_family_percentile is None or not table_available
            else family_percentile > self.signal_config.exclude_bottom_state_family_percentile
        )
        consistency_pass = (
            True if not table_available else family_consistency_score >= self.signal_config.min_family_consistency_score
        )
        mode_pass = True
        if self.signal_config.family_filter_mode == "top_count":
            mode_pass = top_count_pass
        elif self.signal_config.family_filter_mode == "top_percentile":
            mode_pass = top_percentile_pass
        elif self.signal_config.family_filter_mode == "exclude_bottom_count":
            mode_pass = bottom_pass
        elif self.signal_config.family_filter_mode == "exclude_bottom_percentile":
            mode_pass = bottom_percentile_pass
        elif self.signal_config.family_filter_mode == "consistency_threshold":
            mode_pass = consistency_pass
        checks = {
            "family_allowed_pass": allowed,
            "family_excluded_pass": excluded,
            "family_top_count_pass": top_count_pass,
            "family_top_percentile_pass": top_percentile_pass,
            "family_bottom_exclusion_pass": bottom_pass,
            "family_bottom_percentile_pass": bottom_percentile_pass,
            "family_consistency_pass": consistency_pass,
            "family_mode_pass": mode_pass,
        }
        return allowed and excluded and mode_pass, mode_pass, checks

    def _evaluate_hard_filters(
        self,
        candidate_direction: SignalDirection,
        candidate_checks: dict[str, object],
        family_list_pass: bool,
        family_mode_pass: bool,
        state_gate_pass: bool,
        transition_gate_pass: bool,
        consistency_gate_pass: bool,
        sample_size_gate_pass: bool,
        legacy_gate_pass: bool,
    ) -> tuple[bool, str | None, str | None]:
        if candidate_direction == SignalDirection.FLAT:
            reason = str(candidate_checks.get("block_reason") or "negative_expected_value")
            return False, "candidate", reason
        if not family_list_pass:
            return False, "family", "excluded_state_family"

        hard_components = self._hard_filter_components()
        if not hard_components:
            if self.signal_config.selection_mode == "soft_scoring_only":
                return True, None, None
            if legacy_gate_pass:
                return True, None, None
            for component, passed in [
                ("family", family_mode_pass),
                ("state", state_gate_pass),
                ("transition", transition_gate_pass),
                ("consistency", consistency_gate_pass),
            ]:
                if not passed:
                    return False, component, f"{component}_gate_failed"
            return False, "gate", "legacy_gate_failed"

        mapping = {
            "state": (state_gate_pass, "state_gate_failed"),
            "transition": (transition_gate_pass, "transition_gate_failed"),
            "consistency": (consistency_gate_pass, "consistency_gate_failed"),
            "family": (family_mode_pass, "family_filter_failed"),
            "sample_size": (sample_size_gate_pass, "minimum_sample_size_failed"),
        }
        for component in hard_components:
            passed, reason = mapping[component]
            if not passed:
                return False, component, reason
        return True, None, None

    def _component_gate_pass(
        self,
        state_gate_pass: bool,
        transition_gate_pass: bool,
        consistency_gate_pass: bool,
    ) -> bool:
        components = self._gate_components()
        if not components:
            return True
        mapping = {
            "state": state_gate_pass,
            "transition": transition_gate_pass,
            "consistency": consistency_gate_pass,
        }
        return all(mapping[component] for component in components)

    def _gate_components(self) -> list[str]:
        if self.signal_config.quality_gate_components:
            return list(self.signal_config.quality_gate_components)
        if self.signal_config.quality_gate_mode == "state":
            return ["state"]
        if self.signal_config.quality_gate_mode == "transition":
            return ["transition"]
        if self.signal_config.quality_gate_mode == "both":
            return ["state", "transition"]
        return []

    def _hard_filter_components(self) -> list[str]:
        return list(self.signal_config.hard_filter_components)

    def _setup_consistency_score(self, state_quality: dict[str, float], transition_quality: dict[str, float]) -> float:
        values = [
            float(record.get("consistency_score", 0.0))
            for record in (state_quality, transition_quality)
            if record
        ]
        return min(values) if values else 0.0

    def _setup_consistency_percentile(
        self,
        state_quality: dict[str, float],
        transition_quality: dict[str, float],
    ) -> float:
        values = [
            float(record.get("consistency_percentile", 0.0))
            for record in (state_quality, transition_quality)
            if record
        ]
        return min(values) if values else 0.0

    def _setup_consistency_rank(
        self,
        state_quality: dict[str, float],
        transition_quality: dict[str, float],
    ) -> int:
        values = [
            self._safe_int(record.get("consistency_rank"), default=1_000_000)
            for record in (state_quality, transition_quality)
            if record
        ]
        return max(values) if values else 1_000_000

    def _setup_sample_size_score(
        self,
        state_quality: dict[str, float],
        transition_quality: dict[str, float],
        observations: int,
    ) -> float:
        values = [
            float(record.get("sample_size_score", 0.0))
            for record in (state_quality, transition_quality)
            if record
        ]
        if values:
            return min(values)
        return float(np.clip(np.log1p(observations) / np.log1p(max(self.markov_config.min_state_observations * 4, 2)), 0.0, 1.0))

    def _setup_trade_count_score(
        self,
        state_quality: dict[str, float],
        transition_quality: dict[str, float],
        observations: int,
    ) -> float:
        return self._setup_sample_size_score(state_quality, transition_quality, observations)

    @staticmethod
    def _record_rank_score(record: dict[str, float], prefix: str) -> float:
        return float(record.get(f"{prefix}_percentile", 0.0)) if record else 0.0

    @staticmethod
    def _initialize_signal_columns(frame: pd.DataFrame) -> pd.DataFrame:
        signal_frame = frame.copy()
        signal_frame["signal"] = SignalDirection.FLAT.value
        signal_frame["candidate_signal"] = SignalDirection.FLAT.value
        signal_frame["signal_score"] = 0.0
        signal_frame["base_signal_score"] = 0.0
        signal_frame["expected_return_est"] = np.nan
        signal_frame["expected_value"] = np.nan
        signal_frame["signal_confidence"] = np.nan
        signal_frame["predicted_next_state"] = None
        signal_frame["transition_accuracy"] = np.nan
        signal_frame["state_family"] = signal_frame.get("state_family", "unknown")
        signal_frame["transition_setup"] = None
        signal_frame["opportunity_score"] = 0.0
        signal_frame["state_quality_score"] = np.nan
        signal_frame["transition_quality_score"] = np.nan
        signal_frame["state_family_quality_score"] = np.nan
        signal_frame["family_rank_score"] = np.nan
        signal_frame["fold_consistency_score"] = np.nan
        signal_frame["sample_size_score"] = np.nan
        signal_frame["trade_count_score"] = np.nan
        signal_frame["tradability_score"] = np.nan
        signal_frame["selection_policy_name"] = None
        signal_frame["selection_mode"] = None
        signal_frame["state_gate_pass"] = False
        signal_frame["transition_gate_pass"] = False
        signal_frame["consistency_gate_pass"] = False
        signal_frame["family_gate_pass"] = False
        signal_frame["gate_passed"] = False
        signal_frame["hard_filter_passed"] = False
        signal_frame["hard_block_reason"] = None
        signal_frame["hard_block_category"] = None
        signal_frame["selection_passed"] = False
        return signal_frame

    @staticmethod
    def _write_snapshot(signal_frame: pd.DataFrame, index, snapshot: SignalSnapshot) -> None:
        signal_frame.loc[index, "signal"] = snapshot.direction.value
        signal_frame.loc[index, "candidate_signal"] = snapshot.candidate_direction.value
        signal_frame.loc[index, "signal_score"] = snapshot.score
        signal_frame.loc[index, "base_signal_score"] = snapshot.base_score
        signal_frame.loc[index, "expected_return_est"] = snapshot.expected_return
        signal_frame.loc[index, "expected_value"] = snapshot.expected_value
        signal_frame.loc[index, "signal_confidence"] = snapshot.confidence
        signal_frame.loc[index, "predicted_next_state"] = snapshot.predicted_next_state
        signal_frame.loc[index, "transition_accuracy"] = snapshot.transition_accuracy
        signal_frame.loc[index, "state_family"] = snapshot.state_family
        signal_frame.loc[index, "transition_setup"] = snapshot.transition_setup
        signal_frame.loc[index, "opportunity_score"] = snapshot.opportunity_score
        signal_frame.loc[index, "state_quality_score"] = snapshot.state_quality_score
        signal_frame.loc[index, "transition_quality_score"] = snapshot.transition_quality_score
        signal_frame.loc[index, "state_family_quality_score"] = snapshot.state_family_quality_score
        signal_frame.loc[index, "family_rank_score"] = snapshot.family_rank_score
        signal_frame.loc[index, "fold_consistency_score"] = snapshot.fold_consistency_score
        signal_frame.loc[index, "sample_size_score"] = snapshot.sample_size_score
        signal_frame.loc[index, "trade_count_score"] = snapshot.trade_count_score
        signal_frame.loc[index, "tradability_score"] = snapshot.tradability_score
        signal_frame.loc[index, "selection_policy_name"] = snapshot.selection_policy_name
        signal_frame.loc[index, "selection_mode"] = snapshot.selection_mode
        signal_frame.loc[index, "state_gate_pass"] = snapshot.state_gate_pass
        signal_frame.loc[index, "transition_gate_pass"] = snapshot.transition_gate_pass
        signal_frame.loc[index, "consistency_gate_pass"] = snapshot.consistency_gate_pass
        signal_frame.loc[index, "family_gate_pass"] = snapshot.family_gate_pass
        signal_frame.loc[index, "gate_passed"] = snapshot.gate_passed
        signal_frame.loc[index, "hard_filter_passed"] = snapshot.hard_filter_passed
        signal_frame.loc[index, "hard_block_reason"] = snapshot.hard_block_reason
        signal_frame.loc[index, "hard_block_category"] = snapshot.hard_block_category
        signal_frame.loc[index, "selection_passed"] = snapshot.selection_passed
        for key, value in snapshot.diagnostics.items():
            signal_frame.loc[index, key] = value

    def _flat_snapshot(self, current_state: str, state_family: str) -> SignalSnapshot:
        return SignalSnapshot(
            direction=SignalDirection.FLAT,
            candidate_direction=SignalDirection.FLAT,
            current_state=current_state,
            expected_return=0.0,
            expected_value=0.0,
            confidence=0.0,
            score=0.0,
            base_score=0.0,
            signal_quality=0.0,
            volatility=0.0,
            liquidity_score=0.0,
            transition_entropy=1.0,
            sample_size=0,
            predicted_next_state=None,
            transition_accuracy=0.0,
            state_family=state_family,
            transition_setup=None,
            opportunity_score=0.0,
            state_quality_score=0.0,
            transition_quality_score=0.0,
            state_family_quality_score=0.0,
            family_rank_score=0.0,
            fold_consistency_score=0.0,
            sample_size_score=0.0,
            trade_count_score=0.0,
            tradability_score=0.0,
            selection_policy_name=self.signal_config.selection_policy_name,
            selection_mode=self.signal_config.selection_mode,
            state_gate_pass=False,
            transition_gate_pass=False,
            consistency_gate_pass=False,
            family_gate_pass=False,
            gate_passed=False,
            hard_filter_passed=False,
            hard_block_reason="no_state_statistics",
            hard_block_category="candidate",
            selection_passed=False,
            diagnostics={},
        )

    @staticmethod
    def _lookup_quality(
        table: pd.DataFrame,
        key_column: str,
        key: str | None,
        asset_class: AssetClass,
    ) -> dict[str, float]:
        if table.empty or key_column not in table.columns or key in {None, "", "None"}:
            return {}
        matches = table.copy()
        if "asset_class" in matches.columns:
            matches = matches.loc[matches["asset_class"].astype(str) == asset_class.value]
        matches = matches.loc[matches[key_column].astype(str) == str(key)]
        if matches.empty:
            return {}
        return matches.iloc[0].to_dict()

    @staticmethod
    def _weighted_average(values: dict[str, float], weights: dict[str, float] | None = None) -> float:
        numerator = 0.0
        denominator = 0.0
        for key, value in values.items():
            weight = float(weights[key]) if weights and key in weights else 1.0
            numerator += max(float(value), 0.0) * weight
            denominator += weight
        if denominator == 0.0:
            return 0.0
        return numerator / denominator

    @staticmethod
    def _bounded_score(value: float) -> float:
        if value <= 0:
            return 0.0
        return float(np.tanh(value * 100.0))

    @staticmethod
    def _safe_float(value: object, default: float = 0.0) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        if np.isnan(parsed):
            return default
        return parsed

    @staticmethod
    def _safe_int(value: object, default: int = 0) -> int:
        try:
            parsed = int(float(value))
        except (TypeError, ValueError):
            return default
        return parsed
