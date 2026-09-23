from __future__ import annotations
import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError
from trader_engine.backtest.engine import BacktestEngine
from trader_engine.core.config import BacktestConfig, FeatureConfig, RiskConfig, WalkForwardConfig, MarkovConfig, SignalConfig
from trader_engine.core.models import AssetClass, UniverseMember
from trader_engine.risk.engine import RiskManager
from trader_engine.features.engine import FeatureEngineer
from trader_engine.analytics.quality import _max_drawdown
from trader_engine.analytics.metrics import compute_performance_metrics
from trader_engine.data.base import normalize_ohlcv
from trader_engine.signals.engine import SignalEngine
from trader_engine.markov.engine import MarkovAnalyzer

def bars(n=4, direction='long', index=None):
    return pd.DataFrame({'open': 100.0, 'high': 101.0, 'low': 99.0, 'close': 100.0, 'volume': 1000000.0, 'state': 'up', 'state_family': 'up', 'signal': direction, 'signal_score': 1.0, 'atr_14': 1.0}, index=pd.date_range('2024-01-01', periods=n) if index is None else index)

def simulate(frames, risk=None, **kwargs):
    config = dict(hold_bars=100, exit_on_state_change=False, exit_on_signal_flip=False, commission_bps=0, slippage_bps_equity=0, use_atr_stop=False)
    config.update(kwargs)
    engine = BacktestEngine(BacktestConfig(**config), RiskManager(risk or RiskConfig(max_position_pct=0.5)), FeatureConfig())
    return engine.run(frames, {s: UniverseMember(s, AssetClass.EQUITY) for s in frames}, 10000.0)

@pytest.mark.parametrize('direction', ['long', 'short'])
def test_terminal_equity_reconciles_all_fills_and_fees(direction):
    result = simulate({'A': bars(direction=direction)}, commission_bps=100, slippage_bps_equity=10)
    terminal = result.equity_curve.iloc[-1]
    assert terminal.equity == pytest.approx(10000 + result.trades.pnl.sum())
    assert terminal.cash == pytest.approx(terminal.equity)
    assert terminal.open_positions == 0
    assert terminal.gross_exposure == 0
    assert result.metrics['total_return'] == pytest.approx(result.trades.pnl.sum() / 10000)

def test_execution_bar_atr_cannot_change_open_quantity_or_initial_stop():
    frame = bars()
    frame.loc[frame.index[1], 'low'] = 95
    risk = RiskConfig(max_position_pct=0.5, volatility_target=0.01)
    first = simulate({'A': frame}, risk, use_atr_stop=True)
    frame.loc[frame.index[1], 'atr_14'] = 10
    second = simulate({'A': frame}, risk, use_atr_stop=True)
    for result in (first, second):
        trade = result.trades.iloc[0]
        assert trade.exit_reason == 'stop_loss'
        assert trade.exit_price == 98
        assert trade.quantity == 50
        assert trade.hold_bars == 1

def test_gap_loss_blocks_pending_new_entry():
    a = bars()
    a.loc[a.index[2]:, ['open', 'high', 'low', 'close']] = [80, 81, 79, 80]
    b = bars()
    b['signal'] = ['flat', 'long', 'long', 'long']
    result = simulate({'A': a, 'B': b}, RiskConfig(max_position_pct=0.5, max_gross_exposure=2, max_daily_loss_pct=0.03))
    orders = result.orders
    assert not ((orders.symbol == 'B') & (orders.kind == 'entry') & (orders.status == 'filled')).any()

def test_daily_halt_latches_through_recovery_and_resets_next_day():
    idx = pd.to_datetime(['2024-01-01 15:00', '2024-01-02 09:30', '2024-01-02 10:00', '2024-01-02 11:00', '2024-01-02 12:00', '2024-01-03 09:30', '2024-01-03 10:00'])
    a = bars(len(idx), index=idx)
    a.loc[idx[2], ['open', 'high', 'low', 'close']] = [90, 91, 89, 90]
    b = bars(len(idx), index=idx)
    b['signal'] = ['flat', 'flat', 'flat', 'long', 'long', 'long', 'long']
    result = simulate({'A': a, 'B': b}, RiskConfig(max_position_pct=0.5, max_gross_exposure=2, max_daily_loss_pct=0.03))
    entries = result.orders.query("symbol == 'B' and kind == 'entry' and status == 'filled'")
    assert len(entries) == 1
    assert entries.iloc[0].timestamp == idx[-1]

def test_gap_stop_cannot_observe_later_high_or_close_state():
    frame = bars()
    frame['signal'] = ['long', 'long', 'flat', 'flat']
    frame.loc[frame.index[2], ['open', 'high', 'low', 'close', 'state', 'state_family']] = [90, 150, 89, 140, 'future', 'future']
    result = simulate({'A': frame}, use_atr_stop=True)
    trade = result.trades.iloc[0]
    assert trade.exit_price == 90
    assert trade.mfe_pct == pytest.approx(0.01)
    assert trade.mae_pct == pytest.approx(-0.1)
    assert trade.exit_state == 'up'
    assert trade.exit_state_family == 'up'

def test_scheduled_open_exit_uses_last_observed_state():
    frame = bars()
    frame['signal'] = ['long', 'flat', 'flat', 'flat']
    frame.loc[frame.index[2], 'state'] = 'future'
    result = simulate({'A': frame}, exit_on_signal_flip=True)
    assert result.trades.iloc[0].exit_state == 'up'

@pytest.mark.parametrize('direction', ['long', 'short'])
def test_fee_aware_entry_respects_gross_limit(direction):
    result = simulate({'A': bars(direction=direction)}, RiskConfig(max_position_pct=1, max_gross_exposure=1), commission_bps=100, slippage_bps_equity=100)
    assert len(result.trades) == 1
    assert result.equity_curve.gross_exposure.max() <= 1 + 1e-10
    if direction == 'long':
        assert result.equity_curve.cash.min() >= -1e-08

@pytest.mark.parametrize('bad_field,bad_value', [('open', 0), ('close', float('inf')), ('low', 102), ('volume', -1)])
def test_invalid_bars_are_rejected(bad_field, bad_value):
    frame = bars()
    frame.loc[frame.index[1], bad_field] = bad_value
    with pytest.raises(ValueError):
        normalize_ohlcv(frame)

def test_backtest_rejects_invalid_ohlc_even_when_ingestion_is_bypassed():
    frame = bars()
    frame.loc[frame.index[1], 'open'] = 0
    with pytest.raises(ValueError):
        simulate({'A': frame})

def test_conflicting_duplicate_bars_are_rejected():
    frame = bars()
    frame = pd.concat([frame, frame.iloc[[0]].assign(close=100.5)])
    with pytest.raises(ValueError):
        normalize_ohlcv(frame)

@pytest.mark.parametrize('model,kwargs', [(WalkForwardConfig, {'step_bars': 0}), (MarkovConfig, {'forward_return_horizon_bars': 0}), (BacktestConfig, {'commission_bps': -1}), (BacktestConfig, {'slippage_bps_equity': 10000}), (RiskConfig, {'max_position_pct': float('nan')}), (RiskConfig, {'max_position_ptc': 0.9}), (FeatureConfig, {'atr_window': 0})])
def test_invalid_configuration_rejected(model, kwargs):
    with pytest.raises(ValidationError):
        model(**kwargs)

@pytest.mark.parametrize('step,expected', [(1, 100), (-1, 0), (0, 50)])
def test_rsi_handles_one_sided_and_flat_prices(step, expected):
    frame = bars(100)
    frame['close'] = 100 + np.arange(100) * step / 10
    frame['open'] = frame.close
    frame['high'] = frame.close + 1
    frame['low'] = frame.close - 1
    features = FeatureEngineer(FeatureConfig()).transform(frame, AssetClass.EQUITY)
    assert features.rsi_14.iloc[-1] == pytest.approx(expected)
    assert features.rsi_14.iloc[:14].isna().all()

def test_drawdown_includes_first_trade_loss():
    assert _max_drawdown(pd.Series([-0.1])) == pytest.approx(0.1)

def test_sortino_uses_target_downside_deviation():
    frame = pd.DataFrame({'equity': [100, 90, 81], 'gross_exposure': [1, 1, 1]}, index=pd.date_range('2024-01-01', periods=3))
    metrics = compute_performance_metrics(frame, pd.DataFrame(), 252)
    assert metrics['sortino'] == pytest.approx(-np.sqrt(252))

def test_historical_signals_do_not_change_when_future_is_appended():
    f = bars(12)
    f['close'] = np.arange(100.0, 112.0)
    f['realized_vol_20'] = 0
    f['dollar_volume_20'] = 100000000.0
    mc = MarkovConfig(forward_return_horizon_bars=2, min_state_observations=1)
    se = SignalEngine(SignalConfig(min_training_bars=2, min_confidence=0, min_expected_value=0, volatility_penalty=0, transaction_cost_bps=0, slippage_bps=0), mc, FeatureConfig())
    member = UniverseMember('A', AssetClass.EQUITY)
    short = se.generate_historical_signals(member, f.iloc[:10], MarkovAnalyzer(mc))
    long = se.generate_historical_signals(member, f, MarkovAnalyzer(mc))
    pd.testing.assert_series_equal(short.signal, long.signal.iloc[:10])
    assert short.signal.iloc[-1] == 'long'

def test_halt_cancels_entries_queued_for_a_later_session():
    a = bars()
    a.loc[a.index[1], ['open', 'high', 'low', 'close']] = [100, 101, 90, 90]
    b = bars(3, index=pd.to_datetime(['2024-01-01', '2024-01-03', '2024-01-04']))
    b['signal'] = ['long', 'flat', 'flat']
    result = simulate({'A': a, 'B': b}, RiskConfig(max_position_pct=0.5, max_gross_exposure=2, max_daily_loss_pct=0.03))
    orders = result.orders.query("symbol == 'B' and kind == 'entry'")
    assert not (orders.status == 'filled').any()
    assert (orders.status == 'cancelled').any()

def test_empty_export_round_trips_through_dashboard(tmp_path):
    from trader_engine.analytics.export import ArtifactStore
    from trader_engine.ui.dashboard import _read_csv
    path = ArtifactStore(tmp_path).write_frame('no_trades.csv', pd.DataFrame())
    assert _read_csv(path).empty

def test_nonfinite_risk_inputs_fail_closed():
    manager = RiskManager(RiskConfig())
    assert manager.target_notional(float('inf'), 100, 1) == 0
    assert not manager.can_open_position(10000, float('nan'), 0, 100, 10000, 10000)

def test_opening_gap_stop_frees_capacity_before_new_entry():
    a = bars()
    a['signal'] = ['long', 'long', 'flat', 'flat']
    a.loc[a.index[2], ['open', 'high', 'low', 'close']] = [97, 98, 96, 97]
    b = bars()
    b['signal'] = ['flat', 'long', 'flat', 'flat']
    result = simulate({'A': a, 'B': b}, RiskConfig(max_position_pct=0.1, max_concurrent_positions=1), use_atr_stop=True)
    entered = result.orders.query("symbol == 'B' and kind == 'entry' and status == 'filled'")
    assert len(entered) == 1
    assert entered.iloc[0].timestamp == a.index[2]

def test_normalization_produces_numeric_ohlcv():
    frame = bars()[['open', 'high', 'low', 'close', 'volume']].astype(str)
    result = normalize_ohlcv(frame)
    assert all((pd.api.types.is_numeric_dtype(dtype) for dtype in result.dtypes))

@pytest.mark.parametrize('direction', ['long', 'short'])
def test_position_cap_uses_post_fee_equity(direction):
    result = simulate({'A': bars(direction=direction)}, RiskConfig(max_position_pct=0.5, max_gross_exposure=1), commission_bps=100, slippage_bps_equity=100)
    assert len(result.trades) == 1
    assert result.equity_curve.gross_exposure.max() <= 0.5 + 1e-10
