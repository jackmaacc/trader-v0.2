from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

from trader_engine.core.models import SignalDirection


@dataclass
class OrderEvent:
    timestamp: str
    symbol: str
    direction: str
    status: str
    price: float | None
    quantity: float | None
    reason: str


class PaperBroker:
    def __init__(self, journal_dir: Path) -> None:
        self.journal_dir = journal_dir
        self.journal_dir.mkdir(parents=True, exist_ok=True)
        self._events: list[OrderEvent] = []

    def submit(
        self,
        timestamp: str,
        symbol: str,
        direction: SignalDirection,
        status: str,
        reason: str,
        price: float | None = None,
        quantity: float | None = None,
    ) -> None:
        self._events.append(
            OrderEvent(
                timestamp=timestamp,
                symbol=symbol,
                direction=direction.value,
                status=status,
                price=price,
                quantity=quantity,
                reason=reason,
            )
        )

    def events_frame(self) -> pd.DataFrame:
        if not self._events:
            return pd.DataFrame(columns=["timestamp", "symbol", "direction", "status", "price", "quantity", "reason"])
        return pd.DataFrame(asdict(event) for event in self._events)
