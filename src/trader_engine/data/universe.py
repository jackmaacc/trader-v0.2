from __future__ import annotations

from trader_engine.core.config import UniverseConfig
from trader_engine.core.models import AssetClass, UniverseMember


class UniverseManager:
    def __init__(self, config: UniverseConfig) -> None:
        self.config = config

    def members(self) -> list[UniverseMember]:
        members: list[UniverseMember] = []
        seen: set[tuple[AssetClass, str]] = set()

        equity_groups = {"base": self.config.equities, **self.config.watchlists, **self.config.index_constituents}
        for tag, symbols in equity_groups.items():
            for symbol in symbols:
                key = (AssetClass.EQUITY, symbol)
                if key in seen:
                    continue
                seen.add(key)
                members.append(
                    UniverseMember(
                        symbol=symbol,
                        asset_class=AssetClass.EQUITY,
                        provider_symbol=self.config.equity_symbol_map.get(symbol),
                        tags=(tag,),
                    )
                )

        for symbol in self.config.crypto:
            key = (AssetClass.CRYPTO, symbol)
            if key in seen:
                continue
            seen.add(key)
            members.append(
                UniverseMember(
                    symbol=symbol,
                    asset_class=AssetClass.CRYPTO,
                    provider_symbol=self.config.crypto_symbol_map.get(symbol),
                    tags=("crypto",),
                )
            )

        return members
