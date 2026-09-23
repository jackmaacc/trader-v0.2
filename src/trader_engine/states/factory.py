from __future__ import annotations

from trader_engine.core.config import FeatureConfig, StateConfig
from trader_engine.states.base import BaseStateModel
from trader_engine.states.contextual import ContextualStateModel
from trader_engine.states.discretized import FeatureBinStateModel
from trader_engine.states.hybrid import HybridRegimeKMeansStateModel
from trader_engine.states.rule_based import CompositeRuleStateModel, RichCompositeRuleStateModel


def build_state_model(state_config: StateConfig, feature_config: FeatureConfig) -> BaseStateModel:
    if state_config.model_name == "composite_rule":
        model: BaseStateModel = CompositeRuleStateModel(state_config=state_config, feature_config=feature_config)
    elif state_config.model_name == "rich_composite_rule":
        model = RichCompositeRuleStateModel(state_config=state_config, feature_config=feature_config)
    elif state_config.model_name == "feature_bins":
        model = FeatureBinStateModel(state_config=state_config)
    elif state_config.model_name == "kmeans":
        from trader_engine.states.clustering import KMeansStateModel

        model = KMeansStateModel(state_config=state_config)
    elif state_config.model_name == "hybrid_regime_kmeans":
        model = HybridRegimeKMeansStateModel(state_config=state_config, feature_config=feature_config)
    else:
        raise ValueError(f"Unsupported state model: {state_config.model_name}")

    if (
        state_config.contextualize_states
        or state_config.append_previous_state
        or state_config.append_state_age_bucket
        or state_config.append_recent_path
        or state_config.append_entry_bias
    ):
        return ContextualStateModel(base_model=model, state_config=state_config)
    return model
