from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from trader_engine.core.models import AssetClass


class StrictConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class LoggingConfig(StrictConfig):
    level: str = "INFO"


class StorageConfig(StrictConfig):
    cache_dir: str = "artifacts/cache"
    output_dir: str = "artifacts/latest"

    def cache_path(self) -> Path:
        return Path(self.cache_dir)

    def output_path(self) -> Path:
        return Path(self.output_dir)


class UniverseConfig(StrictConfig):
    equities: list[str] = Field(default_factory=list)
    crypto: list[str] = Field(default_factory=list)
    watchlists: dict[str, list[str]] = Field(default_factory=dict)
    index_constituents: dict[str, list[str]] = Field(default_factory=dict)
    equity_symbol_map: dict[str, str] = Field(default_factory=dict)
    crypto_symbol_map: dict[str, str] = Field(default_factory=dict)


class DataConfig(StrictConfig):
    historical_provider: Literal["yfinance"] = "yfinance"
    live_provider: Literal["yfinance", "mock"] = "mock"
    start_date: str | date | datetime = "2020-01-01"
    end_date: str | date | datetime | None = None
    interval: str = "1d"
    adjust_prices: bool = True
    use_cache: bool = True
    min_history_bars: int = 250

    def start_date_string(self) -> str:
        return _date_to_string(self.start_date) or "2020-01-01"

    def end_date_string(self) -> str | None:
        return _date_to_string(self.end_date)


class FeatureConfig(StrictConfig):
    return_horizons: list[int] = Field(default_factory=lambda: [1, 3, 5, 10, 20, 60])
    atr_window: int = Field(default=14, gt=0)
    volatility_window: int = Field(default=20, gt=0)
    rsi_window: int = Field(default=14, gt=0)
    moving_average_windows: list[int] = Field(default_factory=lambda: [10, 20, 50, 200])
    zscore_window: int = Field(default=20, gt=0)
    volume_window: int = Field(default=20, gt=0)
    return_zscore_window: int = Field(default=20, gt=0)
    acceleration_pairs: list[list[int]] = Field(default_factory=lambda: [[3, 10], [5, 20], [20, 60]])
    bars_per_year_equity: int = Field(default=252, gt=0)
    bars_per_year_crypto: int = Field(default=365, gt=0)

    @field_validator("return_horizons", "moving_average_windows")
    @classmethod
    def positive_windows(cls, values: list[int]) -> list[int]:
        if not values or any(value <= 0 for value in values):
            raise ValueError("Feature windows must be nonempty and positive")
        return values


class FeatureOverrideConfig(StrictConfig):
    return_horizons: list[int] | None = None
    atr_window: int | None = None
    volatility_window: int | None = None
    rsi_window: int | None = None
    moving_average_windows: list[int] | None = None
    zscore_window: int | None = None
    volume_window: int | None = None
    return_zscore_window: int | None = None
    acceleration_pairs: list[list[int]] | None = None
    bars_per_year_equity: int | None = None
    bars_per_year_crypto: int | None = None


class StateFeatureGroupConfig(StrictConfig):
    name: str
    features: list[str] = Field(default_factory=list)
    reducer: Literal["mean_zscore", "mean"] = "mean_zscore"
    replace_members: bool = True


class StateConfig(StrictConfig):
    model_name: Literal["composite_rule", "rich_composite_rule", "feature_bins", "kmeans", "hybrid_regime_kmeans"] = (
        "composite_rule"
    )
    trend_threshold: float = 0.01
    volatility_zscore_threshold: float = 0.5
    momentum_threshold: float = 0.02
    extension_threshold: float = 1.5
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    short_horizon: int = 5
    medium_horizon: int = 20
    long_horizon: int = 60
    acceleration_threshold: float = 0.002
    vol_regime_threshold: float = 0.15
    atr_move_threshold: float = 1.0
    return_zscore_threshold: float = 1.0
    volume_expansion_threshold: float = 1.25
    volume_contraction_threshold: float = 0.8
    persistence_thresholds: list[int] = Field(default_factory=lambda: [3, 10])
    rich_components: list[str] = Field(
        default_factory=lambda: ["trend", "volatility", "acceleration", "extension", "volume", "persistence"]
    )
    bin_features: list[str] = Field(
        default_factory=lambda: [
            "return_5",
            "return_20",
            "realized_vol_20",
            "ma_gap_50",
            "rsi_14",
            "volume_ratio_20",
        ]
    )
    bin_count: int = 3
    binning_strategy: Literal["quantile", "uniform"] = "quantile"
    bin_feature_bin_counts: dict[str, int] = Field(default_factory=dict)
    bin_disabled_features: list[str] = Field(default_factory=list)
    bin_feature_groups: list[StateFeatureGroupConfig] = Field(default_factory=list)
    bin_merge_sparse_states: bool = False
    bin_min_state_observations: int | None = None
    bin_merge_order: list[str] = Field(default_factory=list)
    bin_merge_label: str = "any"
    cluster_features: list[str] = Field(
        default_factory=lambda: [
            "return_5",
            "return_20",
            "return_60",
            "realized_vol_20",
            "vol_regime_20",
            "ma_gap_20",
            "ma_gap_50",
            "ma_gap_200",
            "rsi_14",
            "volume_ratio_20",
            "trend_persistence",
        ]
    )
    cluster_count: int = 8
    cluster_random_state: int = 7
    cluster_n_init: int = 20
    cluster_max_iter: int = 300
    cluster_min_size: int = 12
    cluster_merge_small_clusters: bool = True
    cluster_dimensionality_reduction: Literal["none", "pca"] = "none"
    cluster_pca_components: int | None = None
    hybrid_anchor: Literal["trend", "volatility"] = "trend"
    hybrid_cluster_count: int | None = None
    hybrid_min_regime_samples: int = 40
    contextualize_states: bool = False
    append_previous_state: bool = False
    previous_state_mode: Literal["group", "full"] = "group"
    append_state_age_bucket: bool = False
    duration_buckets: list[int] = Field(default_factory=lambda: [3, 10])
    append_recent_path: bool = False
    recent_path_length: int = 2
    append_entry_bias: bool = False
    rich_collapse_rsi_zones: bool = False
    rich_collapse_volatility_regimes: bool = False
    rich_collapse_momentum_regimes: bool = False
    rich_merge_sparse_states: bool = False
    rich_min_state_observations: int | None = None
    rich_merge_order: list[str] = Field(default_factory=list)
    rich_merge_label: str = "any"
    sparse_state_warning_threshold: int = 15


class StateOverrideConfig(StrictConfig):
    model_name: Literal["composite_rule", "rich_composite_rule", "feature_bins", "kmeans", "hybrid_regime_kmeans"] | None = None
    trend_threshold: float | None = None
    volatility_zscore_threshold: float | None = None
    momentum_threshold: float | None = None
    extension_threshold: float | None = None
    rsi_overbought: float | None = None
    rsi_oversold: float | None = None
    short_horizon: int | None = None
    medium_horizon: int | None = None
    long_horizon: int | None = None
    acceleration_threshold: float | None = None
    vol_regime_threshold: float | None = None
    atr_move_threshold: float | None = None
    return_zscore_threshold: float | None = None
    volume_expansion_threshold: float | None = None
    volume_contraction_threshold: float | None = None
    persistence_thresholds: list[int] | None = None
    rich_components: list[str] | None = None
    bin_features: list[str] | None = None
    bin_count: int | None = None
    binning_strategy: Literal["quantile", "uniform"] | None = None
    bin_feature_bin_counts: dict[str, int] | None = None
    bin_disabled_features: list[str] | None = None
    bin_feature_groups: list[StateFeatureGroupConfig] | None = None
    bin_merge_sparse_states: bool | None = None
    bin_min_state_observations: int | None = None
    bin_merge_order: list[str] | None = None
    bin_merge_label: str | None = None
    cluster_features: list[str] | None = None
    cluster_count: int | None = None
    cluster_random_state: int | None = None
    cluster_n_init: int | None = None
    cluster_max_iter: int | None = None
    cluster_min_size: int | None = None
    cluster_merge_small_clusters: bool | None = None
    cluster_dimensionality_reduction: Literal["none", "pca"] | None = None
    cluster_pca_components: int | None = None
    hybrid_anchor: Literal["trend", "volatility"] | None = None
    hybrid_cluster_count: int | None = None
    hybrid_min_regime_samples: int | None = None
    contextualize_states: bool | None = None
    append_previous_state: bool | None = None
    previous_state_mode: Literal["group", "full"] | None = None
    append_state_age_bucket: bool | None = None
    duration_buckets: list[int] | None = None
    append_recent_path: bool | None = None
    recent_path_length: int | None = None
    append_entry_bias: bool | None = None
    rich_collapse_rsi_zones: bool | None = None
    rich_collapse_volatility_regimes: bool | None = None
    rich_collapse_momentum_regimes: bool | None = None
    rich_merge_sparse_states: bool | None = None
    rich_min_state_observations: int | None = None
    rich_merge_order: list[str] | None = None
    rich_merge_label: str | None = None
    sparse_state_warning_threshold: int | None = None


class MarkovConfig(StrictConfig):
    transition_horizon_bars: int = Field(default=1, gt=0)
    forward_return_horizon_bars: int = Field(default=5, gt=0)
    min_state_observations: int = Field(default=25, gt=0)
    laplace_smoothing: float = Field(default=1.0, ge=0)
    transition_lookback_bars: int | None = Field(default=None, gt=0)


class MarkovOverrideConfig(StrictConfig):
    transition_horizon_bars: int | None = None
    forward_return_horizon_bars: int | None = None
    min_state_observations: int | None = None
    laplace_smoothing: float | None = None
    transition_lookback_bars: int | None = None


class SignalConfig(StrictConfig):
    min_training_bars: int = Field(default=175, ge=0)
    min_expected_value: float = 0.001
    min_confidence: float = Field(default=0.08, ge=0, le=1)
    transaction_cost_bps: float = Field(default=5.0, ge=0)
    slippage_bps: float = Field(default=5.0, ge=0)
    volatility_penalty: float = Field(default=0.10, ge=0)
    liquidity_threshold: float = Field(default=1_000_000.0, gt=0)
    liquidity_penalty: float = Field(default=0.001, ge=0)
    allow_short_equities: bool = False
    allow_short_crypto: bool = True
    selection_policy_name: str | None = None
    selection_mode: Literal["hard_gate_only", "soft_scoring_only", "hybrid"] = "hard_gate_only"
    quality_gate_mode: Literal["off", "state", "transition", "both"] = "off"
    quality_gate_components: list[Literal["state", "transition", "consistency"]] = Field(default_factory=list)
    hard_filter_components: list[Literal["state", "transition", "consistency", "family", "sample_size"]] = (
        Field(default_factory=list)
    )
    min_state_quality_score: float = 0.0
    min_transition_quality_score: float = 0.0
    min_fold_consistency_score: float = 0.0
    min_sample_size_score: float = 0.0
    min_state_trade_count: int = 0
    min_transition_trade_count: int = 0
    min_state_active_folds: int = 0
    min_transition_active_folds: int = 0
    min_state_positive_fold_fraction: float = 0.0
    min_transition_positive_fold_fraction: float = 0.0
    min_state_quality_percentile: float = 0.0
    min_transition_quality_percentile: float = 0.0
    min_consistency_percentile: float = 0.0
    max_state_quality_rank: int | None = None
    max_transition_quality_rank: int | None = None
    max_consistency_rank: int | None = None
    max_state_expectancy_variance: float | None = None
    max_transition_expectancy_variance: float | None = None
    max_state_sharpe_variance: float | None = None
    max_transition_sharpe_variance: float | None = None
    allowed_state_families: list[str] = Field(default_factory=list)
    excluded_state_families: list[str] = Field(default_factory=list)
    family_filter_mode: Literal[
        "none",
        "top_count",
        "top_percentile",
        "exclude_bottom_count",
        "exclude_bottom_percentile",
        "consistency_threshold",
        "score_only",
    ] = "none"
    top_state_family_count: int | None = None
    top_state_family_percentile: float | None = None
    exclude_bottom_state_family_count: int | None = None
    exclude_bottom_state_family_percentile: float | None = None
    min_family_consistency_score: float = 0.0
    top_n_per_rebalance_date: int | None = None
    top_n_per_asset_class: int | None = None
    top_n_per_model: int | None = None
    selection_score_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "base_signal": 0.20,
            "opportunity_score": 0.0,
            "state_quality": 0.20,
            "transition_quality": 0.20,
            "family_rank": 0.0,
            "fold_consistency": 0.15,
            "sample_size": 0.10,
            "trade_count": 0.0,
            "tradability": 0.15,
        }
    )


class SignalOverrideConfig(StrictConfig):
    min_training_bars: int | None = None
    min_expected_value: float | None = None
    min_confidence: float | None = None
    transaction_cost_bps: float | None = None
    slippage_bps: float | None = None
    volatility_penalty: float | None = None
    liquidity_threshold: float | None = None
    liquidity_penalty: float | None = None
    allow_short_equities: bool | None = None
    allow_short_crypto: bool | None = None
    selection_policy_name: str | None = None
    selection_mode: Literal["hard_gate_only", "soft_scoring_only", "hybrid"] | None = None
    quality_gate_mode: Literal["off", "state", "transition", "both"] | None = None
    quality_gate_components: list[Literal["state", "transition", "consistency"]] | None = None
    hard_filter_components: list[Literal["state", "transition", "consistency", "family", "sample_size"]] | None = None
    min_state_quality_score: float | None = None
    min_transition_quality_score: float | None = None
    min_fold_consistency_score: float | None = None
    min_sample_size_score: float | None = None
    min_state_trade_count: int | None = None
    min_transition_trade_count: int | None = None
    min_state_active_folds: int | None = None
    min_transition_active_folds: int | None = None
    min_state_positive_fold_fraction: float | None = None
    min_transition_positive_fold_fraction: float | None = None
    min_state_quality_percentile: float | None = None
    min_transition_quality_percentile: float | None = None
    min_consistency_percentile: float | None = None
    max_state_quality_rank: int | None = None
    max_transition_quality_rank: int | None = None
    max_consistency_rank: int | None = None
    max_state_expectancy_variance: float | None = None
    max_transition_expectancy_variance: float | None = None
    max_state_sharpe_variance: float | None = None
    max_transition_sharpe_variance: float | None = None
    allowed_state_families: list[str] | None = None
    excluded_state_families: list[str] | None = None
    family_filter_mode: Literal[
        "none",
        "top_count",
        "top_percentile",
        "exclude_bottom_count",
        "exclude_bottom_percentile",
        "consistency_threshold",
        "score_only",
    ] | None = None
    top_state_family_count: int | None = None
    top_state_family_percentile: float | None = None
    exclude_bottom_state_family_count: int | None = None
    exclude_bottom_state_family_percentile: float | None = None
    min_family_consistency_score: float | None = None
    top_n_per_rebalance_date: int | None = None
    top_n_per_asset_class: int | None = None
    top_n_per_model: int | None = None
    selection_score_weights: dict[str, float] | None = None


class RiskConfig(StrictConfig):
    initial_capital: float = Field(default=100_000.0, gt=0)
    max_position_pct: float = Field(default=0.10, gt=0, le=1)
    max_gross_exposure: float = Field(default=1.00, gt=0)
    max_concurrent_positions: int = Field(default=10, gt=0)
    stop_atr_multiple: float = Field(default=2.0, gt=0)
    max_daily_loss_pct: float = Field(default=0.03, gt=0, le=1)
    volatility_target: float | None = Field(default=None, gt=0)
    max_trade_risk_pct: float | None = Field(default=None, gt=0, le=1)
    max_portfolio_stop_risk_pct: float | None = Field(default=None, gt=0, le=1)
    max_pairwise_correlation: float | None = Field(default=None, ge=0, le=1)
    correlation_lookback_bars: int = Field(default=60, ge=2)


class RiskOverrideConfig(StrictConfig):
    initial_capital: float | None = None
    max_position_pct: float | None = None
    max_gross_exposure: float | None = None
    max_concurrent_positions: int | None = None
    stop_atr_multiple: float | None = None
    max_daily_loss_pct: float | None = None
    volatility_target: float | None = None
    max_trade_risk_pct: float | None = Field(default=None, gt=0, le=1)
    max_portfolio_stop_risk_pct: float | None = Field(default=None, gt=0, le=1)
    max_pairwise_correlation: float | None = Field(default=None, ge=0, le=1)
    correlation_lookback_bars: int | None = Field(default=None, ge=2)


class BacktestConfig(StrictConfig):
    hold_bars: int = Field(default=5, gt=0)
    exit_on_state_change: bool = True
    exit_on_signal_flip: bool = True
    use_atr_stop: bool = True
    trailing_stop: bool = False
    commission_bps: float = Field(default=2.0, ge=0, lt=10000)
    slippage_bps_equity: float = Field(default=2.0, ge=0, lt=10000)
    slippage_bps_crypto: float = Field(default=5.0, ge=0, lt=10000)
    annualization_factor: int = Field(default=252, gt=0)


class BacktestOverrideConfig(StrictConfig):
    hold_bars: int | None = None
    exit_on_state_change: bool | None = None
    exit_on_signal_flip: bool | None = None
    use_atr_stop: bool | None = None
    trailing_stop: bool | None = None
    commission_bps: float | None = None
    slippage_bps_equity: float | None = None
    slippage_bps_crypto: float | None = None
    annualization_factor: int | None = None


class ExecutionConfig(StrictConfig):
    paper_enabled: bool = True
    broker_name: str = "paper"
    journal_dir: str = "artifacts/latest/paper"


class DashboardConfig(StrictConfig):
    artifacts_dir: str = "artifacts/latest"


class WalkForwardConfig(StrictConfig):
    enabled: bool = True
    train_bars: int = Field(default=252, gt=0)
    validation_bars: int = Field(default=63, ge=0)
    test_bars: int = Field(default=63, gt=0)
    step_bars: int = Field(default=63, gt=0)
    refit_on_validation_for_test: bool = True
    minimum_symbol_train_bars: int = Field(default=126, gt=0)


class ParameterSweepConfig(StrictConfig):
    enabled: bool = False
    search_space: dict[str, list[bool | int | float | str]] = Field(default_factory=dict)
    max_combinations: int = 64
    rank_metric: str = "mean_test_sharpe"
    top_k: int = 25


class DiagnosticsConfig(StrictConfig):
    top_n_trades: int = 25
    min_group_observations: int = 5


class StateModelComparisonItem(StrictConfig):
    name: str
    description: str | None = None
    selection_policy: str | None = None
    overrides: dict[str, Any] = Field(default_factory=dict)
    local_sensitivity_overrides: list[dict[str, Any]] = Field(default_factory=list)


class SelectionPolicyConfig(StrictConfig):
    description: str | None = None
    signals: SignalOverrideConfig = Field(default_factory=SignalOverrideConfig)
    asset_signals: dict[str, SignalOverrideConfig] = Field(default_factory=dict)


class SelectionPolicyExperiment(StrictConfig):
    name: str
    model_name: str
    description: str | None = None
    category: str | None = None
    selection_policy: str | None = None
    overrides: dict[str, Any] = Field(default_factory=dict)


class SelectionPolicyComparisonConfig(StrictConfig):
    enabled: bool = False
    experiments: list[SelectionPolicyExperiment] = Field(default_factory=list)
    ranking_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "mean_test_sharpe": 0.35,
            "mean_test_total_return": 0.15,
            "mean_test_expectancy": 0.15,
            "positive_fold_rate_test": 0.10,
            "mean_test_trade_count": 0.10,
            "post_cap_trade_count_test": 0.10,
            "selected_candidate_ratio_test": 0.05,
        }
    )


class StateModelComparisonConfig(StrictConfig):
    enabled: bool = False
    models: list[StateModelComparisonItem] = Field(default_factory=list)
    ranking_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "mean_test_sharpe": 0.30,
            "mean_test_total_return": 0.10,
            "mean_test_trade_count": 0.15,
            "effective_state_coverage": 0.15,
            "share_states_above_threshold": 0.10,
            "trade_generating_state_percentage": 0.10,
            "transition_stability": 0.10,
        }
    )


class ExitComparisonProfile(StrictConfig):
    name: str
    description: str | None = None
    overrides: dict[str, Any] = Field(default_factory=dict)


class ExitComparisonConfig(StrictConfig):
    enabled: bool = False
    model_names: list[str] = Field(default_factory=list)
    profiles: list[ExitComparisonProfile] = Field(default_factory=list)
    max_workers: int = 1


class GateAblationConfig(StrictConfig):
    enabled: bool = False
    model_names: list[str] = Field(default_factory=list)
    selection_mode: Literal["hard_gate_only", "soft_scoring_only", "hybrid"] | None = None


class ResearchConfig(StrictConfig):
    walk_forward: WalkForwardConfig = Field(default_factory=WalkForwardConfig)
    parameter_sweep: ParameterSweepConfig = Field(default_factory=ParameterSweepConfig)
    diagnostics: DiagnosticsConfig = Field(default_factory=DiagnosticsConfig)
    selection_policies: dict[str, SelectionPolicyConfig] = Field(default_factory=dict)
    state_model_comparison: StateModelComparisonConfig = Field(default_factory=StateModelComparisonConfig)
    selection_policy_comparison: SelectionPolicyComparisonConfig = Field(default_factory=SelectionPolicyComparisonConfig)
    exit_comparison: ExitComparisonConfig = Field(default_factory=ExitComparisonConfig)
    gate_ablation: GateAblationConfig = Field(default_factory=GateAblationConfig)


class AssetClassOverrideConfig(StrictConfig):
    features: FeatureOverrideConfig = Field(default_factory=FeatureOverrideConfig)
    states: StateOverrideConfig = Field(default_factory=StateOverrideConfig)
    markov: MarkovOverrideConfig = Field(default_factory=MarkovOverrideConfig)
    signals: SignalOverrideConfig = Field(default_factory=SignalOverrideConfig)
    risk: RiskOverrideConfig = Field(default_factory=RiskOverrideConfig)
    backtest: BacktestOverrideConfig = Field(default_factory=BacktestOverrideConfig)


class AssetClassResolvedConfig(StrictConfig):
    features: FeatureConfig
    states: StateConfig
    markov: MarkovConfig
    signals: SignalConfig
    risk: RiskConfig
    backtest: BacktestConfig


class AppConfig(StrictConfig):
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    features: FeatureConfig = Field(default_factory=FeatureConfig)
    states: StateConfig = Field(default_factory=StateConfig)
    markov: MarkovConfig = Field(default_factory=MarkovConfig)
    signals: SignalConfig = Field(default_factory=SignalConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    asset_overrides: dict[str, AssetClassOverrideConfig] = Field(default_factory=dict)

    def resolved_for_asset_class(self, asset_class: AssetClass | str) -> AssetClassResolvedConfig:
        key = asset_class.value if isinstance(asset_class, AssetClass) else str(asset_class)
        override = self.asset_overrides.get(key, AssetClassOverrideConfig())
        merged_signals = _merge_model(self.signals, override.signals)
        policy_name = merged_signals.selection_policy_name
        if policy_name:
            policy = self.research.selection_policies.get(policy_name)
            if policy is not None:
                merged_signals = _merge_model(merged_signals, policy.signals)
                merged_signals = _merge_model(
                    merged_signals,
                    policy.asset_signals.get(key, SignalOverrideConfig()),
                )
        return AssetClassResolvedConfig(
            features=_merge_model(self.features, override.features),
            states=_merge_model(self.states, override.states),
            markov=_merge_model(self.markov, override.markov),
            signals=merged_signals,
            risk=_merge_model(self.risk, override.risk),
            backtest=_merge_model(self.backtest, override.backtest),
        )


def load_config(path: str | Path) -> AppConfig:
    with Path(path).expanduser().open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    return AppConfig.model_validate(payload)


def _date_to_string(value: str | date | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _merge_model(base: BaseModel, override: BaseModel | None) -> BaseModel:
    if override is None:
        return base.model_copy(deep=True)
    payload = base.model_dump(mode="python")
    payload.update(override.model_dump(mode="python", exclude_none=True))
    return type(base).model_validate(payload)
