"""Explicit stock-data selection; no entitlement probing or automatic fallback."""
from dataclasses import dataclass


@dataclass(frozen=True)
class StockFeedConfig:
    """IEX is the paper default; SIP requires an explicit caller selection."""

    feed: str = "iex"

    def __post_init__(self):
        if self.feed not in ("iex", "sip"):
            raise ValueError("Stock feed must be explicitly iex or sip")

    def to_record(self):
        return {
            "feed": self.feed,
            "expected_feed": self.feed,
            "market_data_coverage": "single_exchange_iex" if self.feed == "iex" else "consolidated_sip",
            "restricted_market_data": self.feed == "iex",
            "feed_fallback": False,
        }
