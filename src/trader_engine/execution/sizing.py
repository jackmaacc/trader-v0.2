"""Pure sizing for whole-share, long-only PAPER entries.

Nothing in this module submits orders or activates a profile. The defaults are
an unvalidated experiment configuration, not evidence of a profitable strategy.
Callers must serialize sizing and reservation updates, and supply a reconciled
account snapshot that includes ALL account positions and outstanding buy orders.
Exits deliberately do not pass through this entry-only function.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, localcontext


Number = Decimal | int | float | str
ZERO = Decimal("0")


def _decimal(name: str, value: Number, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, float, str)):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not result.is_finite() or result < ZERO or (positive and result == ZERO):
        condition = "positive" if positive else "nonnegative"
        raise ValueError(f"{name} must be finite and {condition}")
    return result


@dataclass(frozen=True)
class PaperSizingConfig:
    """Dormant, configurable paper profile; dollar fields use account currency.

    Equity fractions are additional caps, so buying power never increases the
    budget. ``per_share_cost_buffer`` reserves cash and modeled risk for costs;
    it does not guarantee actual fees, stop fills, or a maximum realized loss.
    ``max_session_loss=None`` explicitly disables the session P&L budget only;
    position, cash, gross exposure and per-trade modeled risk caps still apply.
    """

    target_notional: Number = Decimal("7500")
    max_position_notional: Number = Decimal("10000")
    max_gross_notional: Number = Decimal("50000")
    max_position_equity_fraction: Number = Decimal("0.10")
    max_gross_equity_fraction: Number = Decimal("0.50")
    risk_per_trade_equity_fraction: Number = Decimal("0.0025")
    max_session_loss: Number | None = Decimal("1000")
    per_share_cost_buffer: Number = Decimal("0.02")

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            if name == "max_session_loss" and getattr(self, name) is None:
                continue
            value = _decimal(
                name, getattr(self, name), positive=name != "per_share_cost_buffer"
            )
            if name.endswith("_fraction") and value > 1:
                raise ValueError(f"{name} must be at most 1")
            object.__setattr__(self, name, value)
        if self.target_notional > self.max_position_notional:
            raise ValueError("target_notional must not exceed max_position_notional")
        if self.max_position_notional > self.max_gross_notional:
            raise ValueError("max_position_notional must not exceed max_gross_notional")
        if self.max_position_equity_fraction > self.max_gross_equity_fraction:
            raise ValueError("position equity fraction must not exceed gross equity fraction")


@dataclass(frozen=True)
class SizingResult:
    quantity: int
    notional: Decimal
    reserved_cash: Decimal
    modeled_stop_loss: Decimal
    risk_budget: Decimal
    limiting_factors: tuple[str, ...]
    rejection_reason: str | None = None

    @property
    def allowed(self) -> bool:
        return self.quantity > 0


def size_long_entry(
    config: PaperSizingConfig,
    *,
    equity: Number,
    cash: Number,
    session_start_equity: Number,
    entry_price: Number,
    stop_price: Number,
    gross_open_notional: Number = ZERO,
    reserved_entry_notional: Number = ZERO,
    symbol_open_notional: Number = ZERO,
    symbol_reserved_notional: Number = ZERO,
    open_stop_risk: Number = ZERO,
    reserved_stop_risk: Number = ZERO,
    reserved_entry_cash: Number | None = None,
) -> SizingResult:
    """Size a new entry at an enforceable buy-limit price, rounded DOWN.

    ``equity`` is actual net liquidation equity, never leveraged buying power.
    ``cash`` is account cash before reservations (negative cash is rejected).
    Gross exposure uses absolute marked position values; reservations cover
    every outstanding buy, including other strategies and unresolved orders.
    Symbol values are subsets of the corresponding account-wide values.
    ``reserved_entry_cash`` includes pending-entry fees; if omitted it defaults
    to reserved notional. Callers using nonzero fee estimates should provide it.
    Stop-risk inputs include modeled mark-to-stop risk and estimated costs.
    When configured, session loss uses marked equity, including unrealized
    losses. Disabling it leaves the independent per-trade risk cap in force.

    Invalid/missing/non-finite inputs raise ValueError. A valid but exhausted
    limit returns zero shares with a reason. This function does not size exits;
    loss/exposure limits must never prevent closing an existing position.
    Stops may slip or gap: modeled_stop_loss is not a loss guarantee.
    """
    if not isinstance(config, PaperSizingConfig):
        raise ValueError("config must be a PaperSizingConfig")
    values = {
        "equity": equity, "cash": cash,
        "session_start_equity": session_start_equity,
        "entry_price": entry_price, "stop_price": stop_price,
        "gross_open_notional": gross_open_notional,
        "reserved_entry_notional": reserved_entry_notional,
        "symbol_open_notional": symbol_open_notional,
        "symbol_reserved_notional": symbol_reserved_notional,
        "open_stop_risk": open_stop_risk, "reserved_stop_risk": reserved_stop_risk,
    }
    positive = {"equity", "session_start_equity", "entry_price", "stop_price"}
    parsed = {name: _decimal(name, value, positive=name in positive)
              for name, value in values.items()}
    if reserved_entry_cash is None:
        reserved_entry_cash = parsed["reserved_entry_notional"]
    reserved_cash = _decimal("reserved_entry_cash", reserved_entry_cash)
    if reserved_cash < parsed["reserved_entry_notional"]:
        raise ValueError("reserved_entry_cash must cover reserved_entry_notional")
    if parsed["stop_price"] >= parsed["entry_price"]:
        raise ValueError("a long entry requires 0 < stop_price < entry_price")
    if parsed["symbol_open_notional"] > parsed["gross_open_notional"]:
        raise ValueError("symbol_open_notional must be included in gross_open_notional")
    if parsed["symbol_reserved_notional"] > parsed["reserved_entry_notional"]:
        raise ValueError("symbol_reserved_notional must be included in reserved_entry_notional")

    # A private context avoids changes to the caller's Decimal rounding mode.
    with localcontext() as context:
        context.prec = 50
        price = parsed["entry_price"]
        equity = parsed["equity"]
        session_loss = max(ZERO, parsed["session_start_equity"] - equity)
        remaining_loss = None
        risk_budget = equity * config.risk_per_trade_equity_fraction
        if config.max_session_loss is not None:
            remaining_loss = max(ZERO, config.max_session_loss - session_loss)
            risk_budget = min(
                risk_budget,
                max(ZERO, remaining_loss - parsed["open_stop_risk"] - parsed["reserved_stop_risk"]),
            )
        position_cap = min(config.max_position_notional, equity * config.max_position_equity_fraction)
        gross_cap = min(config.max_gross_notional, equity * config.max_gross_equity_fraction)
        share_cash = price + config.per_share_cost_buffer
        share_risk = price - parsed["stop_price"] + config.per_share_cost_buffer
        capacities = {
            "target_notional": config.target_notional / price,
            "position_cap": max(ZERO, position_cap - parsed["symbol_open_notional"] - parsed["symbol_reserved_notional"]) / price,
            "gross_cap": max(ZERO, gross_cap - parsed["gross_open_notional"] - parsed["reserved_entry_notional"]) / price,
            "cash": max(ZERO, parsed["cash"] - reserved_cash) / share_cash,
            "stop_risk": risk_budget / share_risk,
        }
        counts = {name: int(value.to_integral_value(rounding=ROUND_FLOOR))
                  for name, value in capacities.items()}
        quantity = min(counts.values())
        limiting = tuple(name for name, count in counts.items() if count == quantity)
        reason = None
        if quantity == 0:
            reason = ("session loss cutoff reached" if remaining_loss == ZERO else
                      "insufficient capacity for one whole share: " + ", ".join(limiting))
        return SizingResult(
            quantity=quantity,
            notional=quantity * price,
            reserved_cash=quantity * share_cash,
            modeled_stop_loss=quantity * share_risk,
            risk_budget=risk_budget,
            limiting_factors=limiting,
            rejection_reason=reason,
        )
