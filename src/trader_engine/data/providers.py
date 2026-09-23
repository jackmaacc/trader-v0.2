from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import yfinance as yf

from trader_engine.core.models import DataRequest, UniverseMember
from trader_engine.data.base import HistoricalDataProvider, LiveDataProvider, normalize_ohlcv
from trader_engine.data.cache import MarketDataCache

LOGGER = logging.getLogger(__name__)


class YFinanceHistoricalProvider(HistoricalDataProvider):
    name = "yfinance"

    def fetch_bars(self, request: DataRequest) -> pd.DataFrame:
        frame = yf.download(
            tickers=request.member.market_symbol,
            start=request.start,
            end=request.end,
            interval=request.interval,
            auto_adjust=request.adjust_prices,
            progress=False,
            threads=False,
        )
        if frame.empty:
            raise ValueError(f"No market data returned for {request.member.symbol}")
        return normalize_ohlcv(frame)


class YFinanceLiveDataProvider(LiveDataProvider):
    name = "yfinance"

    def fetch_latest_bar(self, member: UniverseMember, interval: str) -> pd.Series:
        history = yf.Ticker(member.market_symbol).history(period="5d", interval=interval, auto_adjust=True)
        if history.empty:
            raise ValueError(f"No live market data returned for {member.symbol}")
        normalized = normalize_ohlcv(history)
        return normalized.iloc[-1]


class MockLiveDataProvider(LiveDataProvider):
    name = "mock"

    def fetch_latest_bar(self, member: UniverseMember, interval: str) -> pd.Series:
        raise NotImplementedError("Mock live provider is a placeholder for future feed adapters.")


class MarketDataService:
    def __init__(
        self,
        provider: HistoricalDataProvider,
        cache: MarketDataCache | None = None,
        use_cache: bool = True,
    ) -> None:
        self.provider = provider
        self.cache = cache
        self.use_cache = use_cache

    def load_history(self, request: DataRequest) -> pd.DataFrame:
        if self.use_cache and self.cache is not None:
            cached = self.cache.load(request)
            if cached is not None:
                LOGGER.debug("Cache hit for %s", request.member.symbol)
                return cached

        frame = self.provider.fetch_bars(request)

        if self.use_cache and self.cache is not None:
            self.cache.store(request, frame)

        return frame


def build_historical_provider(name: str) -> HistoricalDataProvider:
    if name == "yfinance":
        return YFinanceHistoricalProvider()
    raise ValueError(f"Unsupported historical provider: {name}")


def build_live_provider(name: str) -> LiveDataProvider:
    if name == "yfinance":
        return YFinanceLiveDataProvider()
    if name == "mock":
        return MockLiveDataProvider()
    raise ValueError(f"Unsupported live provider: {name}")


def build_data_service(cache_dir: Path, provider_name: str, use_cache: bool) -> MarketDataService:
    return MarketDataService(
        provider=build_historical_provider(provider_name),
        cache=MarketDataCache(cache_dir),
        use_cache=use_cache,
    )
