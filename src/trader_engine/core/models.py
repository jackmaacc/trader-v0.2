from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class AssetClass(str, Enum):
    EQUITY = "equity"
    CRYPTO = "crypto"


class SignalDirection(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


@dataclass(frozen=True)
class UniverseMember:
    symbol: str
    asset_class: AssetClass
    provider_symbol: str | None = None
    tags: tuple[str, ...] = ()

    @property
    def market_symbol(self) -> str:
        return self.provider_symbol or self.symbol


@dataclass(frozen=True)
class DataRequest:
    member: UniverseMember
    start: str
    end: str | None
    interval: str
    provider: str
    adjust_prices: bool = True

    def cache_key(self) -> str:
        raw = "|".join(
            [
                self.provider,
                self.member.asset_class.value,
                self.member.symbol,
                self.member.market_symbol,
                self.start,
                self.end or "latest",
                self.interval,
                str(self.adjust_prices),
            ]
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()


@dataclass
class Opportunity:
    symbol: str
    asset_class: AssetClass
    as_of: datetime
    current_state: str
    direction: SignalDirection
    expected_return: float
    expected_value: float
    confidence: float
    score: float
    signal_quality: float
    volatility: float
    liquidity_score: float
    transition_entropy: float
    sample_size: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["asset_class"] = self.asset_class.value
        payload["direction"] = self.direction.value
        payload["as_of"] = self.as_of.isoformat()
        return payload
