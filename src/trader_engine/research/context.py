from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import pandas as pd

from trader_engine.backtest.engine import BacktestEngine
from trader_engine.core.config import AppConfig, AssetClassResolvedConfig
from trader_engine.core.models import AssetClass, DataRequest, UniverseMember
from trader_engine.data.providers import build_data_service
from trader_engine.data.universe import UniverseManager
from trader_engine.features.engine import FeatureEngineer
from trader_engine.markov.engine import MarkovAnalyzer
from trader_engine.risk.engine import RiskManager
from trader_engine.signals.engine import SignalEngine
from trader_engine.states import build_state_model

LOGGER = logging.getLogger(__name__)


@dataclass
class AssetResearchBundle:
    resolved_config: AssetClassResolvedConfig
    feature_engineer: FeatureEngineer
    markov_analyzer: MarkovAnalyzer
    signal_engine: SignalEngine
    risk_manager: RiskManager

    def create_state_model(self) -> Any:
        return build_state_model(self.resolved_config.states, self.resolved_config.features)


@dataclass
class PreparedDataset:
    members_by_symbol: dict[str, UniverseMember]
    raw_by_symbol: dict[str, pd.DataFrame]
    feature_by_symbol: dict[str, pd.DataFrame]
    classified_by_symbol: dict[str, pd.DataFrame]
    state_model_artifacts_by_symbol: dict[str, dict[str, pd.DataFrame | dict]]
    processed_symbols: int
    skipped_symbols: int


class ResearchContext:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.data_service = build_data_service(
            cache_dir=self.config.storage.cache_path(),
            provider_name=self.config.data.historical_provider,
            use_cache=self.config.data.use_cache,
        )
        self.universe_manager = UniverseManager(config.universe)
        self._bundle_cache: dict[AssetClass, AssetResearchBundle] = {}

    def bundle_for(self, asset_class: AssetClass) -> AssetResearchBundle:
        if asset_class not in self._bundle_cache:
            resolved = self.config.resolved_for_asset_class(asset_class)
            self._bundle_cache[asset_class] = AssetResearchBundle(
                resolved_config=resolved,
                feature_engineer=FeatureEngineer(resolved.features),
                markov_analyzer=MarkovAnalyzer(resolved.markov),
                signal_engine=SignalEngine(resolved.signals, resolved.markov, resolved.features),
                risk_manager=RiskManager(resolved.risk),
            )
        return self._bundle_cache[asset_class]

    def load_raw_market_data(self) -> tuple[dict[str, UniverseMember], dict[str, pd.DataFrame], dict[str, int]]:
        members_by_symbol: dict[str, UniverseMember] = {}
        raw_by_symbol: dict[str, pd.DataFrame] = {}
        skipped = 0
        processed = 0

        for member in self.universe_manager.members():
            request = DataRequest(
                member=member,
                start=self.config.data.start_date_string(),
                end=self.config.data.end_date_string(),
                interval=self.config.data.interval,
                provider=self.config.data.historical_provider,
                adjust_prices=self.config.data.adjust_prices,
            )
            try:
                raw = self.data_service.load_history(request)
            except Exception as exc:
                skipped += 1
                LOGGER.warning("Skipping %s: %s", member.symbol, exc)
                continue

            if len(raw) < self.config.data.min_history_bars:
                skipped += 1
                LOGGER.warning("Skipping %s: only %s bars", member.symbol, len(raw))
                continue

            members_by_symbol[member.symbol] = member
            raw_by_symbol[member.symbol] = raw
            processed += 1

        return members_by_symbol, raw_by_symbol, {"processed_symbols": processed, "skipped_symbols": skipped}

    def prepare_dataset(
        self,
        members_by_symbol: dict[str, UniverseMember] | None = None,
        raw_by_symbol: dict[str, pd.DataFrame] | None = None,
    ) -> PreparedDataset:
        loaded_internally = members_by_symbol is None or raw_by_symbol is None
        if members_by_symbol is None or raw_by_symbol is None:
            members_by_symbol, raw_by_symbol, counters = self.load_raw_market_data()
            raw_skipped = counters["skipped_symbols"]
        else:
            raw_skipped = 0

        feature_by_symbol: dict[str, pd.DataFrame] = {}
        classified_by_symbol: dict[str, pd.DataFrame] = {}
        state_model_artifacts_by_symbol: dict[str, dict[str, pd.DataFrame | dict]] = {}
        for symbol, member in members_by_symbol.items():
            raw = raw_by_symbol.get(symbol)
            if raw is None or raw.empty:
                continue
            bundle = self.bundle_for(member.asset_class)
            enriched = bundle.feature_engineer.transform(raw, member.asset_class)
            state_model = bundle.create_state_model()
            classified = state_model.fit_transform(enriched)
            if classified.dropna(subset=["state"]).empty:
                LOGGER.warning("Skipping %s: no valid state observations", member.symbol)
                continue
            feature_by_symbol[symbol] = enriched
            classified_by_symbol[symbol] = classified
            state_model_artifacts_by_symbol[symbol] = state_model.model_artifacts()

        processed = len(classified_by_symbol)
        skipped = raw_skipped + (len(members_by_symbol) - processed)

        return PreparedDataset(
            members_by_symbol={symbol: members_by_symbol[symbol] for symbol in classified_by_symbol},
            raw_by_symbol={symbol: raw_by_symbol[symbol] for symbol in classified_by_symbol},
            feature_by_symbol={symbol: feature_by_symbol[symbol] for symbol in classified_by_symbol},
            classified_by_symbol=classified_by_symbol,
            state_model_artifacts_by_symbol=state_model_artifacts_by_symbol,
            processed_symbols=processed,
            skipped_symbols=skipped,
        )

    def build_backtest_engine(self) -> BacktestEngine:
        backtest_configs = {}
        risk_managers = {}
        feature_configs = {}
        for asset_class in (AssetClass.EQUITY, AssetClass.CRYPTO):
            bundle = self.bundle_for(asset_class)
            backtest_configs[asset_class] = bundle.resolved_config.backtest
            risk_managers[asset_class] = bundle.risk_manager
            feature_configs[asset_class] = bundle.resolved_config.features
        return BacktestEngine(backtest_configs, risk_managers, feature_configs)
