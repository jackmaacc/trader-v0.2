from __future__ import annotations

import pandas as pd

from trader_engine.backtest.engine import BacktestEngine
from trader_engine.core.config import BacktestConfig, FeatureConfig, RiskConfig
from trader_engine.core.models import AssetClass, UniverseMember
from trader_engine.risk.engine import RiskManager


def test_backtest_engine_executes_next_bar_and_closes_on_signal_flip() -> None:
    index = pd.date_range("2024-01-01", periods=6, freq="D")
    frame = pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0, 103.0, 104.0, 105.0],
            "high": [101.0, 102.0, 103.0, 104.0, 105.0, 106.0],
            "low": [99.0, 100.0, 101.0, 102.0, 103.0, 104.0],
            "close": [100.5, 101.5, 102.5, 103.5, 104.5, 105.5],
            "state": ["up"] * 6,
            "signal": ["flat", "long", "long", "flat", "flat", "flat"],
            "signal_score": [0.0, 1.0, 1.0, 0.0, 0.0, 0.0],
            "expected_value": [0.0, 0.02, 0.02, 0.0, 0.0, 0.0],
            "atr_14": [1.0] * 6,
        },
        index=index,
    )

    risk_manager = RiskManager(
        RiskConfig(
            initial_capital=10_000.0,
            max_position_pct=0.5,
            max_gross_exposure=1.0,
            max_concurrent_positions=1,
            stop_atr_multiple=10.0,
            max_daily_loss_pct=0.5,
        )
    )
    engine = BacktestEngine(
        config=BacktestConfig(
            hold_bars=10,
            exit_on_state_change=False,
            exit_on_signal_flip=True,
            commission_bps=0.0,
            slippage_bps_equity=0.0,
            slippage_bps_crypto=0.0,
            annualization_factor=252,
        ),
        risk_manager=risk_manager,
        feature_config=FeatureConfig(),
    )

    result = engine.run(
        frames_by_symbol={"TEST": frame},
        members_by_symbol={"TEST": UniverseMember(symbol="TEST", asset_class=AssetClass.EQUITY)},
        initial_capital=10_000.0,
    )

    assert len(result.trades) == 1
    trade = result.trades.iloc[0]
    assert pd.Timestamp(trade["entry_timestamp"]) == index[2]
    assert pd.Timestamp(trade["exit_timestamp"]) == index[4]
    assert trade["pnl"] > 0
    assert trade["mfe_pct"] > 0
    assert trade["mae_pct"] <= 0
    assert trade["holding_period_bucket"] in {"1", "2-3", "4-5", "6-10", "11+"}
    assert result.metrics["trade_count"] == 1
