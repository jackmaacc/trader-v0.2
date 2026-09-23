from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import isfinite
from typing import Any

import pandas as pd

from trader_engine.analytics.metrics import (
    compute_performance_metrics,
    performance_by_state,
    performance_by_transition,
    subperiod_performance,
)
from trader_engine.core.config import BacktestConfig, FeatureConfig
from trader_engine.core.models import AssetClass, SignalDirection, UniverseMember
from trader_engine.data.base import validate_bars
from trader_engine.risk.engine import RiskManager


@dataclass
class BacktestResult:
    equity_curve: pd.DataFrame
    trades: pd.DataFrame
    orders: pd.DataFrame
    metrics: dict[str, float | int]
    state_performance: pd.DataFrame
    transition_performance: pd.DataFrame
    subperiod_performance: pd.DataFrame


class BacktestEngine:
    def __init__(
        self,
        config: BacktestConfig | dict[AssetClass | str, BacktestConfig],
        risk_manager: RiskManager | dict[AssetClass | str, RiskManager],
        feature_config: FeatureConfig | dict[AssetClass | str, FeatureConfig],
    ) -> None:
        self.default_config, self.config_by_asset_class = self._normalize_asset_mapping(config)
        self.default_risk_manager, self.risk_managers_by_asset_class = self._normalize_asset_mapping(risk_manager)
        self.default_feature_config, self.feature_configs_by_asset_class = self._normalize_asset_mapping(feature_config)
        self.atr_columns = {
            asset_class: f"atr_{resolved_feature_config.atr_window}"
            for asset_class, resolved_feature_config in self.feature_configs_by_asset_class.items()
        }
        self.default_atr_column = f"atr_{self.default_feature_config.atr_window}"

    def run(
        self,
        frames_by_symbol: dict[str, pd.DataFrame],
        members_by_symbol: dict[str, UniverseMember],
        initial_capital: float,
        risk_history_by_symbol: dict[str, pd.Series] | None = None,
    ) -> BacktestResult:
        if not isfinite(initial_capital) or initial_capital <= 0:
            raise ValueError("initial_capital must be finite and positive")
        for symbol, frame in frames_by_symbol.items():
            if symbol not in members_by_symbol:
                raise ValueError(f"Missing instrument metadata for {symbol}")
            if not frame.empty:
                validate_bars(frame, require_volume=False)
        frames = {symbol: frame.sort_index().copy() for symbol, frame in frames_by_symbol.items() if not frame.empty}
        if not frames:
            empty = pd.DataFrame()
            metrics = compute_performance_metrics(empty, empty, self.default_config.annualization_factor)
            return BacktestResult(empty, empty, empty, metrics, empty, empty, empty)

        active_managers = [self._risk_manager_for(m.asset_class) for m in members_by_symbol.values()]
        portfolio_limits = [r.config.max_portfolio_stop_risk_pct for r in active_managers
                            if r.config.max_portfolio_stop_risk_pct is not None]
        portfolio_limit = min(portfolio_limits) if portfolio_limits else None
        for member in members_by_symbol.values():
            risk = self._risk_manager_for(member.asset_class).config
            if (risk.max_trade_risk_pct is not None or portfolio_limit is not None) and not self._config_for(member.asset_class).use_atr_stop:
                raise ValueError('Stop-risk budgets require enabled ATR stops for the covered instruments.')

        close_history = risk_history_by_symbol if risk_history_by_symbol is not None else {symbol: frame['close'] for symbol, frame in frames.items()}
        if any(manager.config.max_pairwise_correlation is not None for manager in [self.default_risk_manager, *self.risk_managers_by_asset_class.values()]):
            for symbol, series in close_history.items():
                if series.index.has_duplicates or not series.index.is_monotonic_increasing:
                    raise ValueError(f'Invalid correlation history ordering for {symbol}')
                if not series.map(lambda value: isfinite(value) and value > 0).all():
                    raise ValueError(f'Invalid correlation prices for {symbol}')

        next_timestamp_lookup = {
            symbol: {frame.index[i]: frame.index[i + 1] for i in range(len(frame.index) - 1)}
            for symbol, frame in frames.items()
        }
        timeline = sorted({timestamp for frame in frames.values() for timestamp in frame.index})

        positions: dict[str, dict[str, Any]] = {}
        pending_orders: dict[pd.Timestamp, list[dict[str, Any]]] = defaultdict(list)
        order_log: list[dict[str, Any]] = []
        trade_log: list[dict[str, Any]] = []
        equity_log: list[dict[str, Any]] = []
        last_prices: dict[str, float] = {}

        cash = initial_capital
        day_start_equity = initial_capital
        current_day = None
        session_halted = False
        risk_managers = [self._risk_manager_for(member.asset_class) for member in members_by_symbol.values()]

        def loss_breached() -> bool:
            equity = self._portfolio_equity(cash, positions, last_prices)
            return equity <= 0 or any(manager.daily_loss_breached(day_start_equity, equity) for manager in risk_managers)

        def refresh_session_halt(timestamp: pd.Timestamp) -> None:
            nonlocal session_halted
            if session_halted or not loss_breached():
                return
            session_halted = True
            for scheduled_at, queued in list(pending_orders.items()):
                for order in queued:
                    if order["kind"] == "entry":
                        order_log.append(self._order_log_record(
                            timestamp, order, "cancelled", None, None,
                            "Daily loss halt cancelled the pending entry.",
                        ))
                pending_orders[scheduled_at] = [order for order in queued if order["kind"] != "entry"]

        for timestamp in timeline:
            row_map = {symbol: frame.loc[timestamp] for symbol, frame in frames.items() if timestamp in frame.index}
            current_equity = self._portfolio_equity(cash, positions, last_prices)
            if current_day != timestamp.date():
                current_day = timestamp.date()
                day_start_equity = current_equity
                session_halted = False

            # Mark at the opening event before any new risk is accepted.
            for symbol, row in row_map.items():
                last_prices[symbol] = float(row["open"])
            refresh_session_halt(timestamp)

            # Protective orders crossed at the open execute before new entries.
            for symbol, position in list(positions.items()):
                row = row_map.get(symbol)
                if row is None or not self._config_for(position["asset_class"]).use_atr_stop:
                    continue
                opening_row = self._price_only_row(float(row["open"]))
                triggered, price = self._stop_execution_price(position, opening_row)
                if triggered:
                    self._update_position_excursions(position, opening_row)
                    cash = self._close_position(timestamp, opening_row, positions.pop(symbol), cash,
                                                trade_log, order_log, "stop_loss", forced_price=price)
                    refresh_session_halt(timestamp)

            due_orders = pending_orders.pop(timestamp, [])
            exit_orders = [order for order in due_orders if order["kind"] == "exit"]
            entry_orders = sorted(
                [order for order in due_orders if order["kind"] == "entry"],
                key=lambda payload: payload["score"],
                reverse=True,
            )

            for order in exit_orders:
                row = row_map.get(order["symbol"])
                if row is None or order["symbol"] not in positions:
                    order_log.append(
                        self._order_log_record(
                            timestamp=timestamp,
                            order=order,
                            status="skipped",
                            price=None,
                            quantity=None,
                            notes="No active position or no bar for exit execution.",
                        )
                    )
                    continue
                self._update_position_excursions(positions[order["symbol"]], self._price_only_row(float(row["open"])))
                cash = self._close_position(
                    timestamp=timestamp,
                    row=row,
                    position=positions.pop(order["symbol"]),
                    cash=cash,
                    trade_log=trade_log,
                    order_log=order_log,
                    reason=order["reason"],
                )
                refresh_session_halt(timestamp)

            current_equity = self._portfolio_equity(cash, positions, last_prices)
            gross_exposure = self._gross_exposure(positions, last_prices, current_equity)

            for order in entry_orders:
                row = row_map.get(order["symbol"])
                if row is None:
                    order_log.append(
                        self._order_log_record(
                            timestamp=timestamp,
                            order=order,
                            status="rejected",
                            price=None,
                            quantity=None,
                            notes="No bar available for entry execution.",
                        )
                    )
                    continue
                if order["symbol"] in positions:
                    order_log.append(
                        self._order_log_record(
                            timestamp=timestamp,
                            order=order,
                            status="rejected",
                            price=None,
                            quantity=None,
                            notes="Position already active.",
                        )
                    )
                    continue

                asset_class = order["asset_class"]
                risk_manager = self._risk_manager_for(asset_class)
                correlation_rejection = risk_manager.correlation_rejection(
                    order['symbol'], order['direction'], positions, close_history, order['generated_at'],
                )
                if correlation_rejection:
                    order_log.append(self._order_log_record(timestamp, order, 'rejected', None, None, correlation_rejection))
                    continue
                atr = order["signal_atr"]
                open_price = float(row["open"])
                fill_price = self._apply_slippage(open_price, order["direction"], asset_class, is_entry=True)
                # Size a stop budget using the same execution price as the fill,
                # including short-side slippage, rather than the unadjusted open.
                sizing_price = fill_price if risk_manager.config.max_trade_risk_pct is not None else open_price
                proposed_notional = risk_manager.target_notional(current_equity, sizing_price, atr)
                mark_ratio = open_price / fill_price
                commission_rate = self._config_for(asset_class).commission_bps / 10_000.0
                cost_rate = commission_rate + abs(1.0 - mark_ratio)
                if portfolio_limit is not None:
                    current_marks = {symbol: float(bar['open']) for symbol, bar in row_map.items()}
                    committed_risk = RiskManager.open_stop_risk(positions, current_marks)
                    stop = risk_manager.stop_price(fill_price, atr, order['direction'] == SignalDirection.LONG)
                    stop_fraction = abs(fill_price - stop) / fill_price
                    if not isfinite(committed_risk) or not isfinite(stop_fraction) or stop_fraction <= 0 or stop <= 0:
                        order_log.append(self._order_log_record(timestamp, order, 'rejected', None, None,
                            'Portfolio stop-risk budget unavailable: invalid stop or missing current price.'))
                        continue
                    available_risk = max(0.0, portfolio_limit * current_equity - committed_risk)
                    budget_notional = available_risk / (stop_fraction + portfolio_limit * cost_rate)
                    if budget_notional <= 1e-8:
                        order_log.append(self._order_log_record(timestamp, order, 'rejected', None, None,
                            'Portfolio stop-risk budget exhausted.'))
                        continue
                    if budget_notional < proposed_notional:
                        order_log.append(self._order_log_record(timestamp, order, 'risk_adjusted', fill_price,
                            budget_notional / fill_price, 'Position reduced to fit portfolio stop-risk budget.'))
                    proposed_notional = min(proposed_notional, budget_notional)
                gross_notional = gross_exposure * current_equity if current_equity > 0 else float("inf")
                leverage = risk_manager.config.max_gross_exposure
                capacity = max(0.0, (leverage * current_equity - gross_notional) / (mark_ratio + leverage * cost_rate))
                position_limit = risk_manager.config.max_position_pct
                position_capacity = max(0.0, position_limit * current_equity / (mark_ratio + position_limit * cost_rate))
                proposed_notional = min(proposed_notional, capacity, position_capacity)
                if order["direction"] == SignalDirection.LONG and leverage <= 1.0:
                    proposed_notional = min(proposed_notional, max(cash, 0.0) / (1.0 + commission_rate))
                projected_equity = current_equity - cost_rate * proposed_notional
                if session_halted or projected_equity <= 0 or not risk_manager.can_open_position(
                    equity=projected_equity,
                    gross_exposure=gross_notional / projected_equity,
                    open_positions=len(positions),
                    proposed_notional=mark_ratio * proposed_notional,
                    day_start_equity=day_start_equity,
                    current_equity=projected_equity,
                ):
                    order_log.append(
                        self._order_log_record(
                            timestamp=timestamp,
                            order=order,
                            status="rejected",
                            price=None,
                            quantity=None,
                            notes="Risk constraints blocked the trade.",
                        )
                    )
                    continue

                cash, position, order_record = self._open_position(
                    timestamp=timestamp,
                    row=row,
                    order=order,
                    cash=cash,
                    proposed_notional=proposed_notional,
                )
                positions[order["symbol"]] = position
                order_log.append(order_record)
                current_equity = self._portfolio_equity(cash, positions, last_prices)
                gross_exposure = self._gross_exposure(positions, last_prices, current_equity)
                refresh_session_halt(timestamp)

            for symbol, position in list(positions.items()):
                row = row_map.get(symbol)
                if row is None:
                    continue
                stop_triggered, stop_price = (False, 0.0)
                if self._config_for(position["asset_class"]).use_atr_stop:
                    stop_triggered, stop_price = self._stop_execution_price(position=position, row=row)
                if not stop_triggered:
                    self._update_position_excursions(position, row)
                    continue
                # OHLC cannot locate extrema relative to a stop. Report only observed
                # opening/fill excursion bounds, explicitly marked as censored.
                self._update_position_excursions(position, self._price_only_row(float(row["open"])))
                self._update_position_excursions(position, self._price_only_row(stop_price))
                position["excursion_censored"] = True
                position["bars_held"] += 1
                cash = self._close_position(
                    timestamp=timestamp,
                    row=row,
                    position=positions.pop(symbol),
                    cash=cash,
                    trade_log=trade_log,
                    order_log=order_log,
                    reason="stop_loss",
                    forced_price=stop_price,
                )
                refresh_session_halt(timestamp)

            for symbol, row in row_map.items():
                last_prices[symbol] = float(row["close"])
                if symbol in positions:
                    positions[symbol]["bars_held"] += 1
                    positions[symbol]["last_state"] = str(row.get("state", positions[symbol]["last_state"]))
                    positions[symbol]["last_state_family"] = str(row.get("state_family", positions[symbol]["last_state_family"]))
                    self._update_trailing_stop(positions[symbol], row)

            current_equity = self._portfolio_equity(cash, positions, last_prices)
            gross_exposure = self._gross_exposure(positions, last_prices, current_equity)
            refresh_session_halt(timestamp)
            equity_log.append(
                {
                    "timestamp": timestamp,
                    "equity": current_equity,
                    "cash": cash,
                    "gross_exposure": gross_exposure,
                    "open_positions": len(positions),
                    "modeled_stop_risk_dollars": (
                        RiskManager.open_stop_risk(positions, {s: float(bar['close']) for s, bar in row_map.items()})
                        if all(self._config_for(p['asset_class']).use_atr_stop for p in positions.values()) else float('nan')
                    ),
                }
            )

            for symbol, row in row_map.items():
                next_timestamp = next_timestamp_lookup.get(symbol, {}).get(timestamp)
                if next_timestamp is None:
                    continue

                signal = self._direction_from_value(row.get("signal", SignalDirection.FLAT.value))
                member = members_by_symbol[symbol]
                risk_manager = self._risk_manager_for(member.asset_class)
                trading_halted = session_halted
                position = positions.get(symbol)
                if position:
                    asset_config = self._config_for(position["asset_class"])
                    exit_reason = None
                    if asset_config.exit_on_state_change and str(row.get("state")) != position["entry_state"]:
                        exit_reason = "state_change"
                    elif position["bars_held"] >= asset_config.hold_bars:
                        exit_reason = "time_exit"
                    elif asset_config.exit_on_signal_flip and signal != position["direction"]:
                        exit_reason = "signal_flip"

                    if exit_reason and not self._has_pending_order(pending_orders[next_timestamp], symbol, "exit"):
                        pending_orders[next_timestamp].append(
                            self._build_order(
                                kind="exit",
                                symbol=symbol,
                                member=members_by_symbol[symbol],
                                direction=SignalDirection.FLAT,
                                timestamp=timestamp,
                                row=row,
                                reason=exit_reason,
                            )
                        )

                    if (
                        exit_reason
                        and signal in {SignalDirection.LONG, SignalDirection.SHORT}
                        and signal != position["direction"]
                        and not trading_halted
                        and not self._has_pending_order(pending_orders[next_timestamp], symbol, "entry")
                    ):
                        pending_orders[next_timestamp].append(
                            self._build_order(
                                kind="entry",
                                symbol=symbol,
                                member=members_by_symbol[symbol],
                                direction=signal,
                                timestamp=timestamp,
                                row=row,
                                reason="signal_reentry",
                            )
                        )
                elif (
                    not trading_halted
                    and signal in {SignalDirection.LONG, SignalDirection.SHORT}
                    and not self._has_pending_order(pending_orders[next_timestamp], symbol, "entry")
                ):
                    pending_orders[next_timestamp].append(
                        self._build_order(
                            kind="entry",
                            symbol=symbol,
                            member=members_by_symbol[symbol],
                            direction=signal,
                            timestamp=timestamp,
                            row=row,
                            reason="signal_entry",
                        )
                    )

        if timeline:
            final_timestamp = timeline[-1]
            for symbol, position in list(positions.items()):
                price = last_prices.get(symbol, position["entry_price"])
                synthetic_row = pd.Series({"open": price, "close": price, "high": price, "low": price, "state": position["last_state"]})
                cash = self._close_position(
                    timestamp=final_timestamp,
                    row=synthetic_row,
                    position=positions.pop(symbol),
                    cash=cash,
                    trade_log=trade_log,
                    order_log=order_log,
                    reason="end_of_test",
                    forced_price=price,
                )
            equity_log.append(
                {
                    "timestamp": final_timestamp,
                    "equity": cash,
                    "cash": cash,
                    "gross_exposure": 0.0,
                    "open_positions": 0,
                    "modeled_stop_risk_dollars": 0.0,
                }
            )

        equity_curve = pd.DataFrame(equity_log).drop_duplicates(subset=["timestamp"], keep="last").set_index("timestamp")
        trades = pd.DataFrame(trade_log)
        orders = pd.DataFrame(order_log)
        metrics = compute_performance_metrics(equity_curve, trades, self.default_config.annualization_factor)
        state_performance = performance_by_state(trades)
        transition_performance = performance_by_transition(trades)
        subperiod = subperiod_performance(trades)
        return BacktestResult(
            equity_curve=equity_curve,
            trades=trades,
            orders=orders,
            metrics=metrics,
            state_performance=state_performance,
            transition_performance=transition_performance,
            subperiod_performance=subperiod,
        )

    def _build_order(
        self,
        kind: str,
        symbol: str,
        member: UniverseMember,
        direction: SignalDirection,
        timestamp: pd.Timestamp,
        row: pd.Series,
        reason: str,
    ) -> dict[str, Any]:
        return {
            "kind": kind,
            "symbol": symbol,
            "asset_class": member.asset_class,
            "model_name": str(row.get("model_name", "")) or None,
            "direction": direction,
            "generated_at": timestamp,
            "signal_state": str(row.get("state", "unknown")),
            "signal_atr": self._safe_float(row.get(self._atr_column_for(member.asset_class)), default=0.0),
            "score": self._safe_float(row.get("signal_score"), default=0.0),
            "expected_value": self._safe_float(row.get("expected_value"), default=0.0),
            "expected_return_est": self._safe_float(row.get("expected_return_est"), default=0.0),
            "signal_confidence": self._safe_float(row.get("signal_confidence"), default=0.0),
            "predicted_next_state": str(row.get("predicted_next_state", "")) or None,
            "transition_setup": str(row.get("transition_setup", "")) or None,
            "transition_accuracy": self._safe_float(row.get("transition_accuracy"), default=0.0),
            "state_family": str(row.get("state_family", "unknown")),
            "opportunity_score": self._safe_float(row.get("opportunity_score"), default=0.0),
            "state_quality_score": self._safe_float(row.get("state_quality_score"), default=0.0),
            "transition_quality_score": self._safe_float(row.get("transition_quality_score"), default=0.0),
            "state_family_quality_score": self._safe_float(row.get("state_family_quality_score"), default=0.0),
            "family_rank_score": self._safe_float(row.get("family_rank_score"), default=0.0),
            "fold_consistency_score": self._safe_float(row.get("fold_consistency_score"), default=0.0),
            "sample_size_score": self._safe_float(row.get("sample_size_score"), default=0.0),
            "trade_count_score": self._safe_float(row.get("trade_count_score"), default=0.0),
            "tradability_score": self._safe_float(row.get("tradability_score"), default=0.0),
            "candidate_signal": str(row.get("candidate_signal", SignalDirection.FLAT.value)),
            "selection_policy_name": str(row.get("selection_policy_name", "")) or None,
            "selection_mode": str(row.get("selection_mode", "hard_gate_only")),
            "state_gate_pass": bool(row.get("state_gate_pass", False)),
            "transition_gate_pass": bool(row.get("transition_gate_pass", False)),
            "consistency_gate_pass": bool(row.get("consistency_gate_pass", False)),
            "family_gate_pass": bool(row.get("family_gate_pass", False)),
            "gate_passed": bool(row.get("gate_passed", False)),
            "hard_filter_passed": bool(row.get("hard_filter_passed", False)),
            "hard_block_reason": str(row.get("hard_block_reason", "")) or None,
            "hard_block_category": str(row.get("hard_block_category", "")) or None,
            "selection_passed": bool(row.get("selection_passed", False)),
            "cap_passed": bool(row.get("cap_passed", True)),
            "final_selection_passed": bool(row.get("final_selection_passed", False)),
            "selection_rank": self._safe_float(row.get("selection_rank"), default=0.0),
            "asset_class_rank": self._safe_float(row.get("asset_class_rank"), default=0.0),
            "model_rank": self._safe_float(row.get("model_rank"), default=0.0),
            "volatility_regime": str(row.get("state_volatility", "unknown")),
            "reason": reason,
            **self._prefixed_fields(row, "score_component_"),
            **self._prefixed_fields(row, "score_contribution_"),
        }

    @staticmethod
    def _has_pending_order(orders: list[dict[str, Any]], symbol: str, kind: str) -> bool:
        return any(order["symbol"] == symbol and order["kind"] == kind for order in orders)

    def _open_position(
        self,
        timestamp: pd.Timestamp,
        row: pd.Series,
        order: dict[str, Any],
        cash: float,
        proposed_notional: float,
    ) -> tuple[float, dict[str, Any], dict[str, Any]]:
        direction = order["direction"]
        is_long = direction == SignalDirection.LONG
        raw_price = float(row["open"])
        fill_price = self._apply_slippage(
            price=raw_price,
            direction=direction,
            asset_class=order["asset_class"],
            is_entry=True,
        )
        quantity = proposed_notional / fill_price if fill_price > 0 else 0.0
        entry_notional = quantity * fill_price
        asset_config = self._config_for(order["asset_class"])
        commission = entry_notional * asset_config.commission_bps / 10_000.0

        if is_long:
            cash -= entry_notional + commission
        else:
            cash += entry_notional - commission

        risk_manager = self._risk_manager_for(order["asset_class"])
        atr = order["signal_atr"]
        position = {
            "symbol": order["symbol"],
            "asset_class": order["asset_class"],
            "model_name": order.get("model_name"),
            "direction": direction,
            "quantity": quantity,
            "entry_price": fill_price,
            "entry_timestamp": timestamp,
            "entry_state": order["signal_state"],
            "last_state": order["signal_state"],
            "last_state_family": order["state_family"],
            "bars_held": 0,
            "entry_notional": entry_notional,
            "entry_commission": commission,
            "generated_at": order["generated_at"],
            "expected_value": order["expected_value"],
            "expected_return_est": order["expected_return_est"],
            "signal_confidence": order["signal_confidence"],
            "predicted_next_state": order["predicted_next_state"],
            "transition_setup": order["transition_setup"],
            "transition_accuracy": order["transition_accuracy"],
            "state_family": order["state_family"],
            "opportunity_score": order.get("opportunity_score", 0.0),
            "state_quality_score": order["state_quality_score"],
            "transition_quality_score": order["transition_quality_score"],
            "state_family_quality_score": order["state_family_quality_score"],
            "family_rank_score": order.get("family_rank_score", 0.0),
            "fold_consistency_score": order["fold_consistency_score"],
            "sample_size_score": order["sample_size_score"],
            "trade_count_score": order.get("trade_count_score", 0.0),
            "tradability_score": order["tradability_score"],
            "candidate_signal": order["candidate_signal"],
            "selection_policy_name": order.get("selection_policy_name"),
            "selection_mode": order["selection_mode"],
            "state_gate_pass": order["state_gate_pass"],
            "transition_gate_pass": order["transition_gate_pass"],
            "consistency_gate_pass": order["consistency_gate_pass"],
            "family_gate_pass": order["family_gate_pass"],
            "gate_passed": order["gate_passed"],
            "hard_filter_passed": order.get("hard_filter_passed", False),
            "hard_block_reason": order.get("hard_block_reason"),
            "hard_block_category": order.get("hard_block_category"),
            "selection_passed": order["selection_passed"],
            "cap_passed": order["cap_passed"],
            "final_selection_passed": order["final_selection_passed"],
            "selection_rank": order["selection_rank"],
            "asset_class_rank": order["asset_class_rank"],
            "model_rank": order["model_rank"],
            "volatility_regime": order["volatility_regime"],
            "mae_pct": 0.0,
            "mfe_pct": 0.0,
            "stop_price": risk_manager.stop_price(fill_price, atr, is_long=is_long),
            "initial_stop_risk_dollars": quantity * abs(fill_price - risk_manager.stop_price(fill_price, atr, is_long=is_long)) if asset_config.use_atr_stop else float('nan'),
            **self._prefixed_fields(order, "score_component_"),
            **self._prefixed_fields(order, "score_contribution_"),
        }
        order_record = self._order_log_record(
            timestamp=timestamp,
            order=order,
            status="filled",
            price=fill_price,
            quantity=quantity,
            notes="Entry filled.",
        )
        return cash, position, order_record

    def _close_position(
        self,
        timestamp: pd.Timestamp,
        row: pd.Series,
        position: dict[str, Any],
        cash: float,
        trade_log: list[dict[str, Any]],
        order_log: list[dict[str, Any]],
        reason: str,
        forced_price: float | None = None,
    ) -> float:
        direction = position["direction"]
        raw_price = float(forced_price if forced_price is not None else row["open"])
        fill_price = self._apply_slippage(
            price=raw_price,
            direction=direction,
            asset_class=position["asset_class"],
            is_entry=False,
        )
        quantity = position["quantity"]
        exit_notional = quantity * fill_price
        asset_config = self._config_for(position["asset_class"])
        commission = exit_notional * asset_config.commission_bps / 10_000.0

        if direction == SignalDirection.LONG:
            cash += exit_notional - commission
            pnl = quantity * (fill_price - position["entry_price"]) - position["entry_commission"] - commission
        else:
            cash -= exit_notional + commission
            pnl = quantity * (position["entry_price"] - fill_price) - position["entry_commission"] - commission

        realized_return = pnl / position["entry_notional"] if position["entry_notional"] else 0.0
        trade_log.append(
            {
                "symbol": position["symbol"],
                "asset_class": position["asset_class"].value,
                "model_name": position.get("model_name"),
                "direction": direction.value,
                "entry_timestamp": position["entry_timestamp"],
                "exit_timestamp": timestamp,
                "generated_at": position["generated_at"],
                "entry_price": position["entry_price"],
                "exit_price": fill_price,
                "quantity": quantity,
                "entry_notional": position["entry_notional"],
                "initial_stop_risk_dollars": position['initial_stop_risk_dollars'],
                "exit_notional": exit_notional,
                "pnl": pnl,
                "return_pct": realized_return,
                "hold_bars": position["bars_held"],
                "holding_period_bucket": self._holding_period_bucket(position["bars_held"]),
                "entry_state": position["entry_state"],
                "exit_state": position["last_state"],
                "transition_type": f"{position['entry_state']} -> {position['last_state']}",
                "entry_expected_value": position["expected_value"],
                "entry_expected_return": position["expected_return_est"],
                "entry_signal_confidence": position["signal_confidence"],
                "entry_predicted_next_state": position["predicted_next_state"],
                "entry_transition_setup": position["transition_setup"],
                "entry_transition_accuracy": position["transition_accuracy"],
                "entry_state_family": position["state_family"],
                "entry_opportunity_score": position.get("opportunity_score", 0.0),
                "entry_state_quality_score": position["state_quality_score"],
                "entry_transition_quality_score": position["transition_quality_score"],
                "entry_state_family_quality_score": position["state_family_quality_score"],
                "entry_family_rank_score": position.get("family_rank_score", 0.0),
                "entry_fold_consistency_score": position["fold_consistency_score"],
                "entry_sample_size_score": position["sample_size_score"],
                "entry_trade_count_score": position.get("trade_count_score", 0.0),
                "entry_tradability_score": position["tradability_score"],
                "entry_candidate_signal": position["candidate_signal"],
                "entry_selection_policy_name": position.get("selection_policy_name"),
                "entry_selection_mode": position["selection_mode"],
                "entry_state_gate_pass": position["state_gate_pass"],
                "entry_transition_gate_pass": position["transition_gate_pass"],
                "entry_consistency_gate_pass": position["consistency_gate_pass"],
                "entry_family_gate_pass": position["family_gate_pass"],
                "entry_gate_passed": position["gate_passed"],
                "entry_hard_filter_passed": position.get("hard_filter_passed", False),
                "entry_hard_block_reason": position.get("hard_block_reason"),
                "entry_hard_block_category": position.get("hard_block_category"),
                "entry_selection_passed": position["selection_passed"],
                "entry_cap_passed": position["cap_passed"],
                "entry_final_selection_passed": position["final_selection_passed"],
                "entry_selection_rank": position["selection_rank"],
                "entry_asset_class_rank": position["asset_class_rank"],
                "entry_model_rank": position["model_rank"],
                "entry_volatility_regime": position["volatility_regime"],
                "mae_pct": position.get("mae_pct", 0.0),
                "mfe_pct": position.get("mfe_pct", 0.0),
                "favorable_excursion_left_uncaptured_pct": max(position.get("mfe_pct", 0.0) - realized_return, 0.0),
                "expected_vs_realized_gap": (
                    realized_return - position["expected_value"]
                    if position["entry_notional"]
                    else 0.0
                ),
                "exit_state_family": position["last_state_family"],
                "excursion_censored": position.get("excursion_censored", False),
                "exit_reason": reason,
                **self._prefixed_fields(position, "score_component_", target_prefix="entry_"),
                **self._prefixed_fields(position, "score_contribution_", target_prefix="entry_"),
            }
        )
        order_log.append(
            {
                "timestamp": timestamp,
                "symbol": position["symbol"],
                "asset_class": position["asset_class"].value,
                "model_name": position.get("model_name"),
                "kind": "exit",
                "direction": SignalDirection.FLAT.value,
                "status": "filled",
                "price": fill_price,
                "quantity": quantity,
                "signal_state": position["entry_state"],
                "expected_value": position["expected_value"],
                "expected_return_est": position["expected_return_est"],
                "signal_confidence": position["signal_confidence"],
                "predicted_next_state": position["predicted_next_state"],
                "transition_setup": position["transition_setup"],
                "transition_accuracy": position["transition_accuracy"],
                "state_family": position["state_family"],
                "opportunity_score": position.get("opportunity_score", 0.0),
                "state_quality_score": position["state_quality_score"],
                "transition_quality_score": position["transition_quality_score"],
                "state_family_quality_score": position["state_family_quality_score"],
                "family_rank_score": position.get("family_rank_score", 0.0),
                "fold_consistency_score": position["fold_consistency_score"],
                "sample_size_score": position["sample_size_score"],
                "trade_count_score": position.get("trade_count_score", 0.0),
                "tradability_score": position["tradability_score"],
                "candidate_signal": position["candidate_signal"],
                "selection_policy_name": position.get("selection_policy_name"),
                "selection_mode": position["selection_mode"],
                "state_gate_pass": position["state_gate_pass"],
                "transition_gate_pass": position["transition_gate_pass"],
                "consistency_gate_pass": position["consistency_gate_pass"],
                "family_gate_pass": position["family_gate_pass"],
                "gate_passed": position["gate_passed"],
                "hard_filter_passed": position.get("hard_filter_passed", False),
                "hard_block_reason": position.get("hard_block_reason"),
                "hard_block_category": position.get("hard_block_category"),
                "selection_passed": position["selection_passed"],
                "cap_passed": position["cap_passed"],
                "final_selection_passed": position["final_selection_passed"],
                "selection_rank": position["selection_rank"],
                "asset_class_rank": position["asset_class_rank"],
                "model_rank": position["model_rank"],
                "volatility_regime": position["volatility_regime"],
                "reason": reason,
                "notes": "Exit filled.",
                **self._prefixed_fields(position, "score_component_"),
                **self._prefixed_fields(position, "score_contribution_"),
            }
        )
        return cash

    @staticmethod
    def _price_only_row(price: float) -> pd.Series:
        return pd.Series({"open": price, "high": price, "low": price, "close": price})

    def _stop_execution_price(self, position: dict[str, Any], row: pd.Series) -> tuple[bool, float]:
        stop_price = float(position["stop_price"])
        open_price = float(row["open"])
        if position["direction"] == SignalDirection.LONG and float(row["low"]) <= stop_price:
            execution_price = open_price if open_price <= stop_price else stop_price
            return True, execution_price
        if position["direction"] == SignalDirection.SHORT and float(row["high"]) >= stop_price:
            execution_price = open_price if open_price >= stop_price else stop_price
            return True, execution_price
        return False, stop_price

    def _update_trailing_stop(self, position: dict[str, Any], row: pd.Series) -> None:
        asset_config = self._config_for(position["asset_class"])
        if not asset_config.trailing_stop:
            return
        risk_manager = self._risk_manager_for(position["asset_class"])
        atr = self._safe_float(row.get(self._atr_column_for(position["asset_class"])), default=0.0)
        close_price = float(row["close"])
        is_long = position["direction"] == SignalDirection.LONG
        candidate = risk_manager.stop_price(close_price, atr, is_long=is_long)
        if is_long:
            position["stop_price"] = max(float(position["stop_price"]), float(candidate))
        else:
            position["stop_price"] = min(float(position["stop_price"]), float(candidate))

    @staticmethod
    def _update_position_excursions(position: dict[str, Any], row: pd.Series) -> None:
        entry_price = float(position["entry_price"])
        if entry_price <= 0:
            return

        high = float(row.get("high", row.get("close", entry_price)))
        low = float(row.get("low", row.get("close", entry_price)))
        if position["direction"] == SignalDirection.LONG:
            adverse = (low - entry_price) / entry_price
            favorable = (high - entry_price) / entry_price
        else:
            adverse = (entry_price - high) / entry_price
            favorable = (entry_price - low) / entry_price

        position["mae_pct"] = min(float(position.get("mae_pct", 0.0)), adverse)
        position["mfe_pct"] = max(float(position.get("mfe_pct", 0.0)), favorable)

    def _portfolio_equity(
        self,
        cash: float,
        positions: dict[str, dict[str, Any]],
        last_prices: dict[str, float],
    ) -> float:
        equity = cash
        for symbol, position in positions.items():
            price = last_prices.get(symbol, position["entry_price"])
            sign = 1.0 if position["direction"] == SignalDirection.LONG else -1.0
            equity += sign * position["quantity"] * price
        return equity

    @staticmethod
    def _gross_exposure(
        positions: dict[str, dict[str, Any]],
        last_prices: dict[str, float],
        equity: float,
    ) -> float:
        if equity <= 0:
            return float("inf") if positions else 0.0
        exposure = 0.0
        for symbol, position in positions.items():
            price = last_prices.get(symbol, position["entry_price"])
            exposure += abs(position["quantity"] * price)
        return exposure / equity

    def _apply_slippage(
        self,
        price: float,
        direction: SignalDirection,
        asset_class: AssetClass,
        is_entry: bool,
    ) -> float:
        asset_config = self._config_for(asset_class)
        slippage_bps = (
            asset_config.slippage_bps_crypto
            if asset_class == AssetClass.CRYPTO
            else asset_config.slippage_bps_equity
        )
        slip = slippage_bps / 10_000.0
        if direction == SignalDirection.LONG and is_entry:
            return price * (1.0 + slip)
        if direction == SignalDirection.LONG and not is_entry:
            return price * (1.0 - slip)
        if direction == SignalDirection.SHORT and is_entry:
            return price * (1.0 - slip)
        if direction == SignalDirection.SHORT and not is_entry:
            return price * (1.0 + slip)
        return price

    @staticmethod
    def _direction_from_value(value: Any) -> SignalDirection:
        try:
            return SignalDirection(str(value))
        except ValueError:
            return SignalDirection.FLAT

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        if not isfinite(parsed):
            return default
        return parsed

    def _config_for(self, asset_class: AssetClass) -> BacktestConfig:
        return self.config_by_asset_class.get(asset_class, self.default_config)

    def _risk_manager_for(self, asset_class: AssetClass) -> RiskManager:
        return self.risk_managers_by_asset_class.get(asset_class, self.default_risk_manager)

    def _atr_column_for(self, asset_class: AssetClass) -> str:
        return self.atr_columns.get(asset_class, self.default_atr_column)

    @staticmethod
    def _normalize_asset_mapping(value: Any) -> tuple[Any, dict[AssetClass, Any]]:
        if isinstance(value, dict):
            normalized = {
                (key if isinstance(key, AssetClass) else AssetClass(str(key))): item
                for key, item in value.items()
            }
            default = next(iter(normalized.values()))
            return default, normalized
        return value, {}

    @staticmethod
    def _order_log_record(
        timestamp: pd.Timestamp,
        order: dict[str, Any],
        status: str,
        price: float | None,
        quantity: float | None,
        notes: str,
    ) -> dict[str, Any]:
        return {
            "timestamp": timestamp,
            "symbol": order["symbol"],
            "asset_class": order["asset_class"].value,
            "model_name": order.get("model_name"),
            "kind": order["kind"],
            "direction": order["direction"].value,
            "status": status,
            "price": price,
            "quantity": quantity,
            "signal_state": order["signal_state"],
            "expected_value": order["expected_value"],
            "expected_return_est": order.get("expected_return_est"),
            "signal_confidence": order.get("signal_confidence"),
            "predicted_next_state": order.get("predicted_next_state"),
            "transition_setup": order.get("transition_setup"),
            "transition_accuracy": order.get("transition_accuracy"),
            "state_family": order.get("state_family"),
            "opportunity_score": order.get("opportunity_score"),
            "state_quality_score": order.get("state_quality_score"),
            "transition_quality_score": order.get("transition_quality_score"),
            "state_family_quality_score": order.get("state_family_quality_score"),
            "family_rank_score": order.get("family_rank_score"),
            "fold_consistency_score": order.get("fold_consistency_score"),
            "sample_size_score": order.get("sample_size_score"),
            "trade_count_score": order.get("trade_count_score"),
            "tradability_score": order.get("tradability_score"),
            "candidate_signal": order.get("candidate_signal"),
            "selection_policy_name": order.get("selection_policy_name"),
            "selection_mode": order.get("selection_mode"),
            "state_gate_pass": order.get("state_gate_pass"),
            "transition_gate_pass": order.get("transition_gate_pass"),
            "consistency_gate_pass": order.get("consistency_gate_pass"),
            "family_gate_pass": order.get("family_gate_pass"),
            "gate_passed": order.get("gate_passed"),
            "hard_filter_passed": order.get("hard_filter_passed"),
            "hard_block_reason": order.get("hard_block_reason"),
            "hard_block_category": order.get("hard_block_category"),
            "selection_passed": order.get("selection_passed"),
            "cap_passed": order.get("cap_passed"),
            "final_selection_passed": order.get("final_selection_passed"),
            "selection_rank": order.get("selection_rank"),
            "asset_class_rank": order.get("asset_class_rank"),
            "model_rank": order.get("model_rank"),
            "volatility_regime": order.get("volatility_regime"),
            "reason": order["reason"],
            "notes": notes,
            **BacktestEngine._prefixed_fields(order, "score_component_"),
            **BacktestEngine._prefixed_fields(order, "score_contribution_"),
        }

    @staticmethod
    def _prefixed_fields(
        source: dict[str, Any] | pd.Series,
        prefix: str,
        target_prefix: str = "",
    ) -> dict[str, Any]:
        keys = source.index if isinstance(source, pd.Series) else source.keys()
        payload: dict[str, Any] = {}
        for key in keys:
            key_str = str(key)
            if key_str.startswith(prefix):
                payload[f"{target_prefix}{key_str}"] = source[key]
        return payload

    @staticmethod
    def _holding_period_bucket(hold_bars: int) -> str:
        if hold_bars <= 1:
            return "1"
        if hold_bars <= 3:
            return "2-3"
        if hold_bars <= 5:
            return "4-5"
        if hold_bars <= 10:
            return "6-10"
        return "11+"


@dataclass
class ETFReplayResult:
    equity_curve: pd.DataFrame
    trades: pd.DataFrame
    decisions: pd.DataFrame
    positions: dict
    metrics: dict
    symbol_contributions: pd.DataFrame


def _run_etf_replay(frames_by_symbol, spec, initial_capital=100000., *, schedule,
                    daily_signal_frames=None, corporate_actions=None, entry_sessions=None,
                    excluded_symbols=(), execution_delay_minutes=0, calendar_overhead=None):
    """Continuous cash/position replay, one capital initialization and no terminal liquidation.

    Minute index denotes observation/open time; its close becomes known one minute later.
    ``schedule`` has timezone-aware market_open/market_close, one row per actual session.
    Corporate action DataFrame has timestamp index, symbol, split_ratio, cash_dividend
    (distribution per *post-split* share), optional payment_timestamp. Ex-date
    entitlement accrues as a receivable; only an explicit payment timestamp releases
    spendable cash. Unknown payment dates remain receivables, including after sale.
    Signals use separate total-return daily history, raw prices execute. A signal_scale
    converts adjusted daily ATR into the raw price scale at its signal time.
    Missing observations reject entries, but never retrospectively exclude a session.
    """
    from math import floor
    import numpy as np
    from trader_engine.research.etf_candidates import ETFDecision, mean_reversion_session_decisions, momentum_decision
    from trader_engine.research.strategy_spec import ETF_UNIVERSE
    if not isfinite(initial_capital) or initial_capital <= 0:
        raise ValueError('Positive finite initial capital required')
    if set(frames_by_symbol) != set(ETF_UNIVERSE):
        raise ValueError('Supply the complete fixed ETF universe')
    excluded_symbols=tuple(sorted(set(excluded_symbols)))
    if not set(excluded_symbols) < set(ETF_UNIVERSE):
        raise ValueError('Exclusions must be a strict subset of the frozen universe')
    if isinstance(execution_delay_minutes,bool) or not isinstance(execution_delay_minutes,int) or not 0<=execution_delay_minutes<=60:
        raise ValueError('Execution delay must be an integer from 0 through 60 minutes')
    spec_hash=spec.spec_hash
    frames = {}
    for symbol, f in frames_by_symbol.items():
        if not isinstance(f.index, pd.DatetimeIndex) or f.index.tz is None or not f.index.is_monotonic_increasing or f.index.has_duplicates:
            raise ValueError('Ordered unique timezone-aware minute bars required')
        for col in ['open','high','low','close','volume']:
            if col not in f or not np.isfinite(f[col].to_numpy(dtype=float)).all():
                raise ValueError('Invalid OHLCV')
        if (f[['open','high','low','close']] <= 0).any().any() or (f.volume < 0).any():
            raise ValueError('Invalid OHLCV values')
        if ((f.low > f[['open','close']].min(axis=1)) | (f.high < f[['open','close']].max(axis=1))).any():
            raise ValueError('Inconsistent OHLCV')
        frames[symbol] = f
    if spec.family == 'MOM' and (daily_signal_frames is None or set(daily_signal_frames) != set(ETF_UNIVERSE)):
        raise ValueError('MOM requires independent completed daily total-return/ATR history for all ETFs')
    schedule = schedule.copy().sort_values('market_open')
    if schedule.empty:
        raise ValueError('Authoritative session schedule required')
    for c in ['market_open','market_close']:
        schedule[c] = pd.to_datetime(schedule[c], utc=True)
    if (schedule.market_close <= schedule.market_open).any() or schedule.market_open.duplicated().any():
        raise ValueError('Invalid session schedule')
    if any(schedule.market_open.iloc[i] <= schedule.market_close.iloc[i-1] for i in range(1,len(schedule))):
        raise ValueError('Overlapping sessions')
    entry_dates = None if entry_sessions is None else {pd.Timestamp(d).date() for d in entry_sessions}
    actions = pd.DataFrame() if corporate_actions is None else corporate_actions.copy()
    if not actions.empty:
        if actions.index.tz is None or not actions.index.is_monotonic_increasing:
            raise ValueError('Corporate actions require ordered timezone-aware timestamps')
        if not set(actions.symbol).issubset(ETF_UNIVERSE):
            raise ValueError('Unknown corporate action symbol')
        if actions.reset_index().duplicated([actions.index.name or 'index','symbol']).any():
            raise ValueError('Duplicate action event')
    calendar_costs=None
    if calendar_overhead is not None:
        calendar_costs={pd.Timestamp(k).date():float(v) for k,v in calendar_overhead.items()}
        required_dates=set(pd.date_range(schedule.market_open.iloc[0].tz_convert('America/New_York').date(),schedule.market_open.iloc[-1].tz_convert('America/New_York').date()).date)
        if not required_dates.issubset(calendar_costs) or any(not isfinite(v) or v<0 for v in calendar_costs.values()):
            raise ValueError('Calendar overhead requires finite nonnegative costs for every included calendar day')
    charged_dates=set()
    valuation_valid=True;data_complete=True
    action_cursor = 0
    dividend_receivables=[];dividends_paid=0.
    cash = float(initial_capital); positions = {}; marks = {}; trades = []; ledger = []; decisions = []
    realized = {s:0. for s in ETF_UNIVERSE}; gross_realized = {s:0. for s in ETF_UNIVERSE}
    fees = 0.; impact_cost = 0.; overhead = 0.; peak = initial_capital; previous_close = initial_capital
    persistent_halt = False; daily_halt = False
    impact = spec.one_way_impact; commission = spec.commission_rate
    pending = {}; momentum = {}; entered = set()

    def equity():
        return cash + sum(p['qty']*marks[s] for s,p in positions.items()) + sum(r['amount'] for r in dividend_receivables)

    def log(t,s,reason,action='reject',**extra):
        decisions.append(dict(timestamp=t,symbol=s,action=action,reason=reason,spec_hash=spec_hash,**extra))

    def close(s,t,raw,reason):
        nonlocal cash,fees,impact_cost
        p=positions.pop(s); entered.add(s); px=raw*(1-impact); fee=p['qty']*px*commission
        cash+=p['qty']*px-fee; fees+=fee; impact_cost+=p['qty']*(raw-px)
        pnl=p['qty']*px-fee-p['basis']+p['dividends']
        gross=p['qty']*raw-p['raw_basis']+p['dividends']
        realized[s]+=pnl;gross_realized[s]+=gross
        trades.append(dict(symbol=s,entry_time=p['entry_time'],exit_time=t,quantity=p['qty'],
                           pnl=pnl,gross_pnl=gross,fees=p['entry_fee']+fee,reason=reason,
                           entry_session=p['entry_session'],exit_session=current_session))
        log(t,s,reason,'exit',price=px,quantity=p['qty'])

    def halt_check():
        nonlocal persistent_halt,daily_halt,peak
        eq=equity()+overhead;peak=max(peak,eq)
        if eq <= peak*(1-spec.drawdown_halt):persistent_halt=True
        if eq <= previous_close*(1-spec.daily_loss_halt):daily_halt=True

    for session_number, (_, session) in enumerate(schedule.iterrows()):
        opening=session.market_open; closing=session.market_close
        current_session=opening.tz_convert('America/New_York').date()
        cutoff=closing-pd.Timedelta(minutes=5); execution_start=opening+pd.Timedelta(minutes=5+execution_delay_minutes)
        daily_halt=False;entered=set();pending={}
        day_start_business_equity=equity()
        if calendar_costs is not None:
            due_dates={d for d in required_dates if d<=current_session and d not in charged_dates}
            charge=sum(calendar_costs[d] for d in due_dates);charged_dates.update(due_dates)
            cash-=charge;overhead+=charge
        elif spec.daily_overhead is not None:
            cash-=spec.daily_overhead;overhead+=spec.daily_overhead
        # Only previously completed sessions feed today's momentum decision.
        if spec.family == 'MOM':
            momentum={}
            for s,f in daily_signal_frames.items():
                if f.index.has_duplicates or not f.index.is_monotonic_increasing:
                    raise ValueError('Daily signal history must be ordered and unique')
                daily_dates=pd.to_datetime(f.index).date
                completed=f.loc[daily_dates < current_session]
                expected_previous=session.get('previous_session')
                if expected_previous is None or pd.isna(expected_previous):
                    expected_previous=schedule.market_open.iloc[session_number-1].tz_convert('America/New_York').date() if session_number else None
                if expected_previous is None:data_complete=False
                if expected_previous is not None and (completed.empty or pd.Timestamp(completed.index[-1]).date()!=pd.Timestamp(expected_previous).date()):
                    momentum[s]=ETFDecision(False,'stale_daily_history');data_complete=False
                else:
                    momentum[s]=momentum_decision(completed,spec)
                if s not in excluded_symbols:log(execution_start,s,momentum[s].reason,'signal' if momentum[s].eligible else 'reject')
        session_frames={s:f.iloc[f.index.searchsorted(opening):f.index.searchsorted(closing)] for s,f in frames.items()}
        minute_maps={s:dict(zip(f.index,f[['open','high','low','close','volume']].itertuples(index=False,name='Bar'))) for s,f in session_frames.items()}
        mr_decisions = {s:mean_reversion_session_decisions(f,spec) for s,f in session_frames.items()} if spec.family=='MR' else {}
        timeline=pd.date_range(opening,closing-pd.Timedelta(minutes=1),freq='min')
        local_times=timeline.tz_convert('America/New_York')
        log_signal_times=set(timeline[(local_times.hour*60+local_times.minute>=630)&(local_times.hour*60+local_times.minute<=870)])
        for t in timeline:
            rows={s:m[t] for s,m in minute_maps.items() if t in m}
            if any(s not in rows for s in ETF_UNIVERSE if s not in excluded_symbols):data_complete=False
            if any(s not in rows for s in positions):valuation_valid=False
            # Actions at or before this observation are known economic ledger events.
            while action_cursor < len(actions) and actions.index[action_cursor] <= t:
                event=actions.iloc[action_cursor];s=event.symbol
                ratio=float(event.get('split_ratio',1.));div=float(event.get('cash_dividend',0.))
                if not isfinite(ratio) or ratio <= 0 or not isfinite(div) or div < 0:
                    raise ValueError('Invalid corporate action')
                if s in positions:
                    p=positions[s];p['qty']*=ratio;p['stop']/=ratio
                    if p['target'] is not None:p['target']/=ratio
                    if s in marks:marks[s]/=ratio
                    credit=p['qty']*div;p['dividends']+=credit
                    payment=event.get('payment_timestamp',pd.NaT)
                    if pd.notna(payment):
                        payment=pd.Timestamp(payment)
                        if payment.tzinfo is None or payment < actions.index[action_cursor]:
                            raise ValueError('Dividend payment must be timezone-aware and no earlier than entitlement')
                    if credit:
                        dividend_receivables.append(dict(symbol=s,amount=credit,payment_timestamp=payment))
                action_cursor+=1
            # Settlement is independent of current ownership: selling does not cancel
            # an already accrued entitlement. Remove paid claims to prevent double cash.
            unpaid=[]
            for claim in dividend_receivables:
                payment=claim['payment_timestamp']
                if pd.notna(payment) and payment<=t:
                    cash+=claim['amount'];dividends_paid+=claim['amount']
                else:unpaid.append(claim)
            dividend_receivables=unpaid
            for s,r in rows.items():marks[s]=float(r.open)
            halt_check()
            for s,p in list(positions.items()):
                if s not in rows:continue
                raw=float(rows[s].open);reason=None
                if raw<=p['stop']:reason='gap_stop'
                elif persistent_halt or daily_halt:reason='drawdown_halt' if persistent_halt else 'daily_loss_halt'
                elif spec.family=='MR' and (t>=cutoff or t-p['entry_time']>=pd.Timedelta(minutes=spec.horizon)):
                    reason='session_flatten' if t>=cutoff else 'time_exit'
                elif spec.family=='MOM' and (session_number-p['entry_session_number']>=5 or (t>=cutoff and session_number-p['entry_session_number']>=4)):reason='five_session_exit'
                elif spec.family=='MOM' and t>=execution_start and momentum[s].reason=='nonpositive_momentum':reason='momentum_exit'
                if reason:close(s,t,raw,reason)
            halt_check()
            candidates=[]
            if spec.family=='MOM' and execution_start<=t<cutoff:
                for s,d in momentum.items():
                    if d.eligible and s not in excluded_symbols and s not in positions and s not in entered and s in rows:candidates.append((s,d))
            elif spec.family=='MR':
                for s,(due,d) in list(pending.items()):
                    if t>=due:
                        del pending[s]
                        if t!=due or s not in rows:
                            log(t,s,'pending_expired_missing_observation');continue
                        if execution_delay_minutes:
                            latest=mr_decisions[s].get(t-pd.Timedelta(minutes=1))
                            if latest is None or not latest.eligible:
                                log(t,s,'delay_revalidation_failed');continue
                        candidates.append((s,d))
            for s,d in sorted(candidates,key=lambda x:(-x[1].score,x[0])):
                if s in excluded_symbols or s in positions or s in entered:continue
                if persistent_halt or daily_halt:log(t,s,'halt');continue
                if entry_dates is not None and current_session not in entry_dates:continue
                if t>=cutoff:log(t,s,'session_cutoff');continue
                if any(k not in rows for k in positions):log(t,s,'held_mark_unavailable');continue
                raw=float(rows[s].open);px=raw*(1+impact);distance=(1.5 if spec.family=='MR' else 3)*d.atr
                if distance<=0 or distance>=px:log(t,s,'invalid_stop');continue
                if d.target is not None and d.target<=px:log(t,s,'target_not_above_entry');continue
                eq=equity();gross=sum(p['qty']*marks[k] for k,p in positions.items())
                risk=sum(_etf_mark_to_stop_risk(p['qty'],marks[k],p['stop'],impact,commission) for k,p in positions.items())
                cluster=sum(p['qty']*marks[k] for k,p in positions.items() if k in ('SPY','QQQ','IWM'))
                cap=min(spec.max_gross,spec.max_overnight) if spec.family=='MOM' else spec.max_gross
                per_cost=px*(1+commission)-raw
                stop_cost=px*(1+commission)-(px-distance)*(1-impact)*(1-commission)
                limits=[cash/(px*(1+commission)),spec.max_trade_risk*eq/stop_cost,
                        max(0,spec.max_portfolio_risk*eq-risk)/(stop_cost+spec.max_portfolio_risk*per_cost),
                        max(0,cap*eq-gross)/(raw+cap*per_cost),spec.max_name*eq/(raw+spec.max_name*per_cost)]
                if s in ('SPY','QQQ','IWM'):limits.append(max(0,spec.max_equity_cluster*eq-cluster)/(raw+spec.max_equity_cluster*per_cost))
                qty=max(0,floor(min(limits)))
                if qty<1 or len(positions)>=5:log(t,s,'risk_capacity');continue
                fee=qty*px*commission;basis=qty*px+fee;cash-=basis;fees+=fee;impact_cost+=qty*(px-raw)
                positions[s]=dict(qty=qty,basis=basis,raw_basis=qty*raw,entry_fee=fee,dividends=0.,stop=px-distance,
                                  target=d.target,entry_time=t,entry_session=current_session,entry_session_number=session_number)
                entered.add(s);log(t,s,'qualified','entry',price=px,quantity=qty)
            # Conservative barrier ordering: stop precedes target within ambiguous OHLC bars.
            for s,p in list(positions.items()):
                if s not in rows:continue
                r=rows[s]
                if r.low<=p['stop']:close(s,t,p['stop'],'stop')
                elif p['target'] is not None and r.high>=p['target']:close(s,t,p['target'],'target')
            for s,r in rows.items():marks[s]=float(r.close)
            halt_check()
            mark_equity=equity();net_pnl=mark_equity+overhead-initial_capital
            ledger.append(dict(timestamp=t+pd.Timedelta(minutes=1),equity=mark_equity,broker_equity=mark_equity+overhead,cash=cash,
                               cumulative_net_pnl=net_pnl,cumulative_gross_pnl=net_pnl+fees+impact_cost,
                               cumulative_fees=fees,cumulative_impact_cost=impact_cost,
                               dividend_receivable=sum(r['amount'] for r in dividend_receivables),dividends_paid=dividends_paid,
                               modeled_stop_risk=sum(_etf_mark_to_stop_risk(p['qty'],marks[s],p['stop'],impact,commission) for s,p in positions.items()),
                               day_start_broker_equity=previous_close,day_start_business_equity=day_start_business_equity,
                               gross_exposure=sum(p['qty']*marks[s] for s,p in positions.items()),
                               open_positions=len(positions),daily_halt=daily_halt,drawdown_halt=persistent_halt,
                               stale_symbols=tuple(s for s in positions if s not in rows),overhead=overhead))
            if spec.family=='MR' and t+pd.Timedelta(minutes=1)<cutoff:
                for s in rows:
                    if s in excluded_symbols or s in entered or s in positions:continue
                    # Only this session's completed bars; no future eligibility scan.
                    d=mr_decisions[s][t]
                    if d.eligible and s not in pending:pending[s]=(t+pd.Timedelta(minutes=1+execution_delay_minutes),d)
                    if t in log_signal_times:
                        log(t+pd.Timedelta(minutes=1),s,d.reason,'signal' if d.eligible else 'reject')
        previous_close=equity()+overhead
    contributions=[]
    for s in ETF_UNIVERSE:
        p=positions.get(s)
        unrealized=0. if p is None else p['qty']*marks[s]-p['basis']+p['dividends']
        gross_unrealized=0. if p is None else p['qty']*marks[s]-p['raw_basis']+p['dividends']
        contributions.append(dict(symbol=s,net_pnl=realized[s]+unrealized,gross_pnl=gross_realized[s]+gross_unrealized))
    net=sum(x['net_pnl'] for x in contributions);gross=sum(x['gross_pnl'] for x in contributions)
    assert abs(equity()-initial_capital-(net-overhead))<1e-6
    metrics=dict(spec_hash=spec_hash,gross_pnl=gross,net_pnl=net,
                 excluded_symbols=excluded_symbols,execution_delay_minutes=execution_delay_minutes,
                 valuation_valid=valuation_valid,data_complete=data_complete,
                 dividend_receivable=sum(r['amount'] for r in dividend_receivables),dividends_paid=dividends_paid,
                 overhead_mode='calendar' if calendar_costs is not None else 'exchange_session',
                 business_pnl=None if spec.daily_overhead is None and calendar_costs is None else net-overhead,
                 overhead_known=spec.daily_overhead is not None or calendar_costs is not None,overhead=overhead,fees=fees,
                 modeled_impact=impact_cost,final_equity=equity(),drawdown_halt=persistent_halt,
                 sessions=len(schedule),open_positions=len(positions),trade_count=len(trades))
    return ETFReplayResult(pd.DataFrame(ledger),pd.DataFrame(trades),pd.DataFrame(decisions),positions,metrics,pd.DataFrame(contributions))


def _etf_mark_to_stop_risk(quantity,mark,stop,impact,commission):
    return quantity*max(mark-stop*(1-impact)*(1-commission),0.)


# Explicit opt-in API preserves the existing event engine and its legacy callers.
BacktestEngine.run_etf_replay = staticmethod(_run_etf_replay)
