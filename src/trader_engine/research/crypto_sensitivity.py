"""Batched sensitivity of the frozen crypto baseline, checked against BacktestEngine.

This is a research proxy, not the IOC paper executor. It retains the baseline's
USD commission approximation, fractional quantities and terminal liquidation;
it omits the paper service's $12,400 cap, venue/queue and ownership mechanics.
Delay shifts observed completed-bar signals, never prices, by whole UTC days.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from trader_engine.backtest.engine import BacktestEngine
from trader_engine.core.config import BacktestConfig, FeatureConfig, RiskConfig
from trader_engine.core.models import AssetClass, UniverseMember
from trader_engine.data.base import validate_bars
from trader_engine.risk.engine import RiskManager

SYMBOLS = ('BTC-USD', 'ETH-USD')
DAILY_FIELDS = ('cash', 'equity', 'exposure', 'cumulative_fees',
                'cumulative_impact', 'gross_pnl', 'net_pnl', 'halted')
FILL_FIELDS = ('trial_index', 'day_index', 'symbol_index', 'side', 'quantity',
               'price', 'raw_price', 'fee', 'reason')


def prepare(frames, start, end, benchmark=False):
    """Freeze full-history SMA200 before slicing; preserve Jan 1 as a cash day."""
    normalized = {}
    signals = []
    dates = None
    for symbol in SYMBOLS:
        frame = frames[symbol].copy()
        frame.index = pd.to_datetime(frame.index, utc=True)
        validate_bars(frame)
        if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
            raise ValueError('Invalid crypto calendar ordering')
        if not frame.index.equals(pd.date_range(frame.index[0], frame.index[-1], freq='D')):
            raise ValueError('Missing crypto daily bars')
        signal = frame.close > frame.close.rolling(200, min_periods=200).mean()
        if benchmark:
            signal[:] = True
        selected = frame.loc[start:end]
        expected = pd.date_range(start, end, freq='D', tz='UTC')
        if not selected.index.equals(expected) or len(frame.loc[:start]) < 201:
            raise ValueError('Incomplete requested calendar or warmup')
        dates = selected.index if dates is None else dates
        normalized[symbol] = selected
        signals.append(np.stack([signal.shift(d, fill_value=False).loc[start:end].to_numpy()
                                 for d in (0, 1)]))
    return dict(dates=dates, open=np.column_stack([normalized[s].open for s in SYMBOLS]),
                close=np.column_stack([normalized[s].close for s in SYMBOLS]),
                signals=np.stack(signals, axis=-1), frames=normalized)


def run_reference(prepared, impact_bps, delay_days, fee_bps=25.):
    if delay_days not in (0, 1):
        raise ValueError('Only frozen signal delays 0/1 supported')
    frames = {}
    for a, symbol in enumerate(SYMBOLS):
        frame = prepared['frames'][symbol].copy()
        frame['signal'] = np.where(prepared['signals'][delay_days, :, a], 'long', 'flat')
        frame['state'] = 'crypto_sma200'
        frame['signal_score'] = 1.
        frames[symbol] = frame
    config = BacktestConfig(hold_bars=100000, exit_on_state_change=False,
                           exit_on_signal_flip=True, use_atr_stop=False,
                           trailing_stop=False, commission_bps=fee_bps,
                           slippage_bps_crypto=impact_bps, annualization_factor=365)
    risk = RiskConfig(max_position_pct=.125, max_gross_exposure=.25,
                      max_concurrent_positions=2, max_daily_loss_pct=.03)
    return BacktestEngine(config, RiskManager(risk), FeatureConfig()).run(
        frames, {s: UniverseMember(s, AssetClass.CRYPTO) for s in SYMBOLS}, 100000.)


def run_batch(prepared, impact_bps, delay_days, fee_bps=25.):
    """Return complete daily ledgers and every actual fill for each scenario."""
    impact = np.asarray(impact_bps, dtype=float) / 10000.
    delay = np.asarray(delay_days)
    if (impact.ndim != 1 or len(impact) == 0 or delay.shape != impact.shape
            or not np.isfinite(impact).all() or np.any((impact < 0) | (impact >= .1))
            or not np.isin(delay, [0, 1]).all() or not 0 <= fee_bps < 1000):
        raise ValueError('Invalid frozen sensitivity assumptions')
    delay = delay.astype(int)
    n = len(impact)
    days = len(prepared['dates'])
    fee_rate = fee_bps / 10000.
    cash = np.full(n, 100000.)
    qty = np.zeros((n, 2))
    pending_entry = np.zeros((n, 2), dtype=bool)
    pending_exit = pending_entry.copy()
    fees = np.zeros(n)
    impact_cost = np.zeros(n)
    previous_equity = cash.copy()
    daily = np.empty((n, days, len(DAILY_FIELDS)))
    fills = []

    def record(mask, d, a, side, q, price, raw, charge, reason):
        ids = np.flatnonzero(mask)
        if len(ids):
            fills.append(pd.DataFrame(dict(trial_index=ids, day_index=d,
                symbol_index=a, side=side, quantity=q[ids], price=price[ids],
                raw_price=raw, fee=charge[ids], reason=reason)))

    def close(mask, d, a, raw, reason):
        nonlocal cash, fees, impact_cost
        q = np.where(mask, qty[:, a], 0.)
        price = raw * (1 - impact)
        notional = q * price
        charge = notional * fee_rate
        cash += notional - charge
        fees += charge
        impact_cost += q * (raw - price)
        record(mask, d, a, 'sell', q, price, raw, charge, reason)
        qty[mask, a] = 0.

    for d in range(days):
        opening = prepared['open'][d]
        closing = prepared['close'][d]
        day_start = previous_equity.copy()
        equity = cash + np.sum(qty * opening, axis=1)
        halted = (equity <= 0) | (equity / day_start - 1 <= -.03)
        # Opening gaps cancel all pending entries; exits are still executed.
        pending_entry[halted] = False
        for a in range(2):
            close(pending_exit[:, a] & (qty[:, a] > 0), d, a, opening[a], 'signal_flip')
            equity = cash + np.sum(qty * opening, axis=1)
            halted |= (equity <= 0) | (equity / day_start - 1 <= -.03)
        for a in range(2):
            equity = cash + np.sum(qty * opening, axis=1)
            gross = np.sum(qty * opening, axis=1)
            price = opening[a] * (1 + impact)
            mark_ratio = opening[a] / price
            cost_rate = fee_rate + np.abs(1 - mark_ratio)
            capacity = np.maximum(0., (.25 * equity - gross) / (mark_ratio + .25 * cost_rate))
            position_capacity = np.maximum(0., .125 * equity / (mark_ratio + .125 * cost_rate))
            notional = np.minimum.reduce([.125 * equity, capacity, position_capacity,
                                          np.maximum(cash, 0.) / (1 + fee_rate)])
            projected = equity - cost_rate * notional
            mask = (pending_entry[:, a] & (qty[:, a] == 0) & ~halted
                    & (projected > 0) & (notional > 0)
                    & (projected / day_start - 1 > -.03)
                    & ((gross + mark_ratio * notional) / projected <= .25 + 1e-12))
            q = np.where(mask, notional / price, 0.)
            # Match engine's actual quantity * price arithmetic, not proposed notional.
            actual = q * price
            charge = actual * fee_rate
            cash -= actual + charge
            fees += charge
            impact_cost += q * (price - opening[a])
            qty[:, a] += q
            record(mask, d, a, 'buy', q, price, opening[a], charge, 'signal_entry')
            equity = cash + np.sum(qty * opening, axis=1)
            halted |= (equity <= 0) | (equity / day_start - 1 <= -.03)
        exposure = np.sum(qty * closing, axis=1)
        equity = cash + exposure
        halted |= (equity <= 0) | (equity / day_start - 1 <= -.03)
        signals = prepared['signals'][delay, d, :]
        pending_exit = (qty > 0) & ~signals
        pending_entry = (qty == 0) & signals & ~halted[:, None]
        if d == days - 1:
            for a in range(2):
                close(qty[:, a] > 0, d, a, closing[a], 'end_of_test')
            exposure = np.zeros(n)
            equity = cash.copy()
        net = equity - 100000.
        daily[:, d, :] = np.column_stack([cash, equity, exposure, fees, impact_cost,
                                         net + fees + impact_cost, net, halted])
        previous_equity = equity
    filled = pd.concat(fills, ignore_index=True) if fills else pd.DataFrame(columns=FILL_FIELDS)
    peak = np.maximum.accumulate(np.maximum(daily[:, :, 1], 100000.), axis=1)
    counts = (filled.loc[filled.side == 'sell'].groupby('trial_index').size()
              .reindex(range(n), fill_value=0))
    metrics = pd.DataFrame(dict(final_equity=equity, net_pnl=net,
        gross_pnl=net + fees + impact_cost, fees=fees, modeled_impact=impact_cost,
        max_drawdown=np.max(1 - daily[:, :, 1] / peak, axis=1),
        average_exposure=np.mean(daily[:, :, 2] / daily[:, :, 1], axis=1),
        completed_trades=counts.to_numpy(), halted_days=daily[:, :, 7].sum(axis=1)))
    return dict(daily=daily, fills=filled, metrics=metrics)


def assert_reference_parity(prepared, impact_bps, delay_days, fee_bps=25., atol=1e-6):
    """Compare every daily cash/equity/exposure value and every filled order."""
    batch = run_batch(prepared, [impact_bps], [delay_days], fee_bps)
    reference = run_reference(prepared, impact_bps, delay_days, fee_bps)
    ledger = batch['daily'][0]
    np.testing.assert_allclose(ledger[:, :2], reference.equity_curve[['cash', 'equity']], rtol=0, atol=atol)
    np.testing.assert_allclose(ledger[:, 2], reference.equity_curve.gross_exposure * reference.equity_curve.equity, rtol=0, atol=atol)
    actual = batch['fills'].copy()
    expected = reference.orders.loc[reference.orders.status == 'filled'].copy() if not reference.orders.empty else reference.orders.copy()
    if len(actual) != len(expected):
        raise AssertionError('Filled order count differs')
    if len(actual):
        expected['day_index'] = expected.timestamp.map({t: i for i, t in enumerate(prepared['dates'])})
        expected['symbol_index'] = expected.symbol.map({s: a for a, s in enumerate(SYMBOLS)})
        expected['side'] = np.where(expected.notes == 'Entry filled.', 'buy', 'sell')
        columns = ['day_index', 'symbol_index', 'side', 'reason']
        actual = actual.sort_values(columns).reset_index(drop=True)
        expected = expected.sort_values(columns).reset_index(drop=True)
        pd.testing.assert_frame_equal(actual[columns], expected[columns], check_dtype=False)
        np.testing.assert_allclose(actual[['quantity', 'price']], expected[['quantity', 'price']], rtol=0, atol=atol)
    # Reconstruct costs independently from shared-engine fills and raw prices.
    # A gross-minus-cost identity alone cannot detect misattributed costs.
    daily_costs = np.zeros((len(ledger), 2))
    for order in expected.itertuples():
        d, a = int(order.day_index), int(order.symbol_index)
        raw = prepared['close' if order.reason == 'end_of_test' else 'open'][d, a]
        daily_costs[d, 0] += order.quantity * order.price * fee_bps / 10000.
        daily_costs[d, 1] += order.quantity * abs(order.price - raw)
    cumulative = np.cumsum(daily_costs, axis=0)
    np.testing.assert_allclose(ledger[:, 3:5], cumulative, rtol=0, atol=atol)
    expected_net = reference.equity_curve.equity.to_numpy() - 100000.
    np.testing.assert_allclose(ledger[:, 5], expected_net + cumulative.sum(axis=1), rtol=0, atol=atol)
    np.testing.assert_allclose(ledger[:, 6], expected_net, rtol=0, atol=atol)
    return dict(days=len(ledger), fills=len(actual), impact_bps=float(impact_bps), delay_days=int(delay_days), parity=True)
