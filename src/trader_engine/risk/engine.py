from __future__ import annotations

from math import isfinite

import pandas as pd

from trader_engine.core.config import RiskConfig


class RiskManager:
    def __init__(self, config: RiskConfig) -> None:
        self.config = config

    def target_notional(self, equity: float, price: float, atr: float | None) -> float:
        if not isfinite(equity) or not isfinite(price) or equity <= 0 or price <= 0:
            return 0.0
        notional = equity * self.config.max_position_pct
        if self.config.volatility_target and atr and atr > 0:
            realized_move = max(atr / price, 1e-4)
            scale = min(1.0, self.config.volatility_target / realized_move)
            notional *= max(scale, 0.1)
        if self.config.max_trade_risk_pct is not None:
            # Match the initial stop's fallback; this budgets modeled stop loss,
            # not gaps, execution costs, or an actual guaranteed maximum loss.
            if atr is not None and not isfinite(atr):
                return 0.0
            effective_atr = atr if atr is not None and atr > 0 else price * 0.02
            stop_fraction = effective_atr / price * self.config.stop_atr_multiple
            notional = min(notional, equity * self.config.max_trade_risk_pct / stop_fraction)
        return max(notional, 0.0)

    def can_open_position(
        self,
        equity: float,
        gross_exposure: float,
        open_positions: int,
        proposed_notional: float,
        day_start_equity: float,
        current_equity: float,
    ) -> bool:
        if not all(isfinite(value) for value in (equity, gross_exposure, proposed_notional, day_start_equity, current_equity)):
            return False
        if equity <= 0 or proposed_notional <= 0:
            return False
        if open_positions >= self.config.max_concurrent_positions:
            return False
        if self.daily_loss_breached(day_start_equity=day_start_equity, current_equity=current_equity):
            return False
        proposed_exposure = gross_exposure + proposed_notional / equity
        return proposed_exposure <= self.config.max_gross_exposure + 1e-12

    def stop_price(self, entry_price: float, atr: float | None, is_long: bool) -> float:
        if not atr or atr <= 0:
            atr = entry_price * 0.02
        distance = atr * self.config.stop_atr_multiple
        return entry_price - distance if is_long else entry_price + distance

    def daily_loss_breached(self, day_start_equity: float, current_equity: float) -> bool:
        if day_start_equity <= 0:
            return False
        return (current_equity / day_start_equity - 1.0) <= -self.config.max_daily_loss_pct


    def correlation_rejection(self, symbol, direction, positions, close_history, as_of):
        """Check exposure correlation using closes available when the order was generated.

        Insufficient/constant history blocks additional positions when enabled.
        The first position needs no pairwise check. Stops/exits are unaffected.
        """
        limit = self.config.max_pairwise_correlation
        if limit is None or not positions:
            return None
        lookback = self.config.correlation_lookback_bars
        candidate = close_history.get(symbol)
        if candidate is None:
            return 'Correlation history missing for candidate.'
        candidate_returns = candidate.loc[:as_of].tail(lookback + 1).pct_change(fill_method=None)
        side = 1 if str(getattr(direction, 'value', direction)) == 'long' else -1
        for held_symbol, position in positions.items():
            held = close_history.get(held_symbol)
            if held is None:
                return 'Correlation history missing for held position.'
            held_returns = held.loc[:as_of].tail(lookback + 1).pct_change(fill_method=None)
            paired = pd.concat([candidate_returns.rename('candidate'), held_returns.rename('held')], axis=1).dropna()
            if len(paired) < lookback:
                return 'Insufficient overlapping correlation history.'
            correlation = float(paired.candidate.corr(paired.held))
            if not isfinite(correlation):
                return 'Undefined correlation from constant or invalid history.'
            held_side = 1 if str(getattr(position['direction'], 'value', position['direction'])) == 'long' else -1
            if correlation * side * held_side > limit + 1e-12:
                return f'Exposure correlation exceeds {limit:.2f} against {held_symbol}.'
        return None

    @staticmethod
    def open_stop_risk(positions, prices) -> float:
        """Sum modeled mark-to-stop losses, never netting long/short losses.

        Missing current marks or invalid stops fail closed. This excludes gaps,
        fees and slippage and does not guarantee a maximum realized loss.
        """
        total = 0.0
        for symbol, position in positions.items():
            price = prices.get(symbol)
            stop = position.get('stop_price')
            quantity = position.get('quantity')
            if any(value is None or not isfinite(value) for value in (price, stop, quantity)):
                return float('inf')
            if price <= 0 or stop <= 0 or quantity < 0:
                return float('inf')
            direction = str(getattr(position['direction'], 'value', position['direction']))
            if direction not in ('long', 'short'):
                return float('inf')
            distance = price - stop if direction == 'long' else stop - price
            total += quantity * max(distance, 0.0)
        return total
