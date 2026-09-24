"""Typed per-symbol quote diagnostics; validation does not submit or cancel orders."""
from __future__ import annotations

import copy
import math
from dataclasses import asdict, dataclass, field
import json
from decimal import Decimal
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class QuotePolicy:
    max_source_age_seconds: float = 3.0
    max_cache_age_seconds: float = 2.0
    max_future_skew_seconds: float = 0.25
    max_spread_bps: float | None = 25.0
    expected_feed: str = "sip"
    require_sizes: bool = True
    min_ask: float = 0.0

    def __post_init__(self):
        values = (self.max_source_age_seconds, self.max_cache_age_seconds,
                  self.max_future_skew_seconds, self.min_ask)
        if self.max_spread_bps is not None:
            values += (self.max_spread_bps,)
        if any(not math.isfinite(v) or v < 0 for v in values):
            raise ValueError("Quote policy values must be finite and nonnegative")
        if self.expected_feed not in {"sip", "iex"}:
            raise ValueError("Explicit supported feed required")


@dataclass(frozen=True)
class QuoteEnvelope:
    symbol: str
    feed: str
    received_at: str
    decision_at: str
    raw_payload: Any


@dataclass(frozen=True)
class QuoteValidationResult:
    envelope: QuoteEnvelope
    valid: bool
    reasons: tuple[str, ...]
    event_at: str | None = None
    bid: float | None = None
    ask: float | None = None
    source_age_seconds: float | None = None
    cache_age_seconds: float | None = None
    spread_bps: float | None = None

    policy: QuotePolicy = field(default_factory=QuotePolicy)

    def to_record(self):
        record = asdict(self)
        # Preserve nonstandard NaN/Infinity tokens separately while keeping the
        # evidence strict JSON for durable journals and shadow artifacts.
        raw = record["envelope"]["raw_payload"]
        try:
            json.dumps(raw, allow_nan=False)
        except ValueError:
            record["raw_payload_json"] = json.dumps(raw, allow_nan=True)
            record["raw_payload_encoding"] = "nonfinite-json-tokens"
            record["envelope"]["raw_payload"] = json.loads(
                record["raw_payload_json"], parse_constant=lambda token: {"nonfinite_float": token})
        return record


def _stamp(value):
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError("Quote timestamps require explicit timezone")
    return stamp.tz_convert("UTC")


def _number(value):
    if isinstance(value, bool):
        raise ValueError("Boolean quote value")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Nonfinite quote value")
    return number


def validate_quote(envelope: QuoteEnvelope, policy: QuotePolicy | None = None, *, expected_symbol=None):
    policy = policy or QuotePolicy()
    envelope = copy.deepcopy(envelope)
    reasons = []
    if expected_symbol is not None and envelope.symbol != expected_symbol:
        reasons.append("quote_symbol_mismatch")
    raw = envelope.raw_payload
    source_age = cache_age = spread = bid = ask = None
    event_at = None
    if envelope.feed != policy.expected_feed:
        reasons.append("unexpected_feed")
    try:
        received, decision = _stamp(envelope.received_at), _stamp(envelope.decision_at)
        cache_age = (decision - received).total_seconds()
        if cache_age < -policy.max_future_skew_seconds:
            reasons.append("receipt_after_decision")
        if cache_age > policy.max_cache_age_seconds:
            reasons.append("stale_cache")
    except (ValueError, TypeError, OverflowError):
        received = decision = None
        reasons.append("invalid_local_timestamp")
    if raw is None:
        reasons.append("missing_quote")
    elif not isinstance(raw, dict):
        reasons.append("invalid_quote_payload")
    else:
        try:
            event = _stamp(raw.get("t"))
            event_at = event.isoformat()
            if decision is not None:
                source_age = (decision - event).total_seconds()
                if source_age < -policy.max_future_skew_seconds:
                    reasons.append("future_event")
                if source_age > policy.max_source_age_seconds:
                    reasons.append("stale_source")
                if (event - received).total_seconds() > policy.max_future_skew_seconds:
                    reasons.append("event_after_receipt")
        except (ValueError, TypeError, OverflowError):
            reasons.append("invalid_event_timestamp")
        try:
            bid, ask = _number(raw["bp"]), _number(raw["ap"])
            if bid <= 0 or ask <= 0:
                reasons.append("nonpositive_price")
            elif bid > ask:
                reasons.append("crossed_quote")
            else:
                exact_bid, exact_ask = Decimal(str(raw["bp"])), Decimal(str(raw["ap"]))
                exact_spread = (exact_ask - exact_bid) / ((exact_ask + exact_bid) / 2) * 10000
                spread = float(exact_spread)
                if policy.max_spread_bps is not None and exact_spread > Decimal(str(policy.max_spread_bps)):
                    reasons.append("excessive_spread")
        except (KeyError, ValueError, TypeError, ArithmeticError):
            reasons.append("invalid_price")
        if ask is not None and ask < policy.min_ask:
            reasons.append("ask_below_minimum")
        for field_name in (("bs", "as") if policy.require_sizes else ()):
            try:
                if _number(raw[field_name]) <= 0:
                    reasons.append("nonpositive_" + field_name)
            except (KeyError, ValueError, TypeError, ArithmeticError):
                reasons.append("invalid_" + field_name)
    return QuoteValidationResult(envelope, not reasons, tuple(reasons), event_at,
                                 bid, ask, source_age, cache_age, spread, policy)


def validate_quote_batch(payload, symbols, *, received_at, decision_at, feed="sip",
                         policy: QuotePolicy | None = None):
    """A malformed/missing symbol cannot suppress another symbol's diagnostic."""
    symbols = tuple(symbols)
    if len(set(symbols)) != len(symbols):
        raise ValueError("Unique symbols required")
    quotes = payload.get("quotes") if isinstance(payload, dict) else None
    output = {}
    for symbol in symbols:
        raw = quotes.get(symbol) if isinstance(quotes, dict) else None
        envelope = QuoteEnvelope(symbol, feed, str(received_at), str(decision_at), copy.deepcopy(raw))
        result = validate_quote(envelope, policy)
        if not isinstance(quotes, dict):
            result = QuoteValidationResult(envelope, False,
                                           ("invalid_batch_payload",) + result.reasons,
                                           result.event_at, result.bid, result.ask,
                                           result.source_age_seconds, result.cache_age_seconds,
                                           result.spread_bps, result.policy)
        output[symbol] = result
    return output

