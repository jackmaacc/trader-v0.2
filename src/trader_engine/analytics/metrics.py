from __future__ import annotations

import math

import numpy as np
import pandas as pd


def compute_performance_metrics(
    equity_curve: pd.DataFrame,
    trades: pd.DataFrame,
    annualization_factor: int,
) -> dict[str, float | int]:
    if equity_curve.empty:
        return {
            "total_return": 0.0,
            "cagr": 0.0,
            "sharpe": 0.0,
            "sortino": 0.0,
            "max_drawdown": 0.0,
            "calmar": 0.0,
            "win_rate": 0.0,
            "average_trade_return": 0.0,
            "average_win": 0.0,
            "average_loss": 0.0,
            "expectancy": 0.0,
            "exposure_adjusted_return": 0.0,
            "turnover": 0.0,
            "trade_count": 0,
            "average_hold_bars": 0.0,
        }

    equity = equity_curve["equity"].astype(float)
    returns = equity.pct_change().dropna()
    total_return = float(equity.iloc[-1] / equity.iloc[0] - 1.0)

    years = _years_in_sample(equity_curve.index, annualization_factor)
    cagr = float((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1.0) if years > 0 else 0.0

    volatility = returns.std()
    downside = np.sqrt(returns.clip(upper=0.0).pow(2).mean())
    sharpe = float(np.sqrt(annualization_factor) * returns.mean() / volatility) if volatility and not np.isnan(volatility) else 0.0
    sortino = (
        float(np.sqrt(annualization_factor) * returns.mean() / downside)
        if downside and not np.isnan(downside)
        else 0.0
    )

    drawdown = equity / equity.cummax() - 1.0
    max_drawdown = float(drawdown.min()) if not drawdown.empty else 0.0
    calmar = float(cagr / abs(max_drawdown)) if max_drawdown < 0 else 0.0

    win_rate = float((trades["pnl"] > 0).mean()) if not trades.empty else 0.0
    avg_trade = float(trades["return_pct"].mean()) if not trades.empty else 0.0
    avg_win = float(trades.loc[trades["pnl"] > 0, "return_pct"].mean()) if not trades.empty else 0.0
    avg_loss = float(trades.loc[trades["pnl"] < 0, "return_pct"].mean()) if not trades.empty else 0.0
    expectancy = avg_trade

    avg_exposure = float(equity_curve["gross_exposure"].mean()) if "gross_exposure" in equity_curve else 0.0
    exposure_adjusted_return = float(total_return / avg_exposure) if avg_exposure > 0 else 0.0

    turnover = 0.0
    if not trades.empty and "entry_notional" in trades and "exit_notional" in trades:
        turnover = float((trades["entry_notional"].abs() + trades["exit_notional"].abs()).sum() / equity.mean())

    trade_count = int(len(trades))
    average_hold_bars = float(trades["hold_bars"].mean()) if not trades.empty else 0.0

    return {
        "total_return": total_return,
        "cagr": cagr,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_drawdown,
        "calmar": calmar,
        "win_rate": win_rate,
        "average_trade_return": avg_trade,
        "average_win": avg_win if not math.isnan(avg_win) else 0.0,
        "average_loss": avg_loss if not math.isnan(avg_loss) else 0.0,
        "expectancy": expectancy,
        "exposure_adjusted_return": exposure_adjusted_return,
        "turnover": turnover,
        "trade_count": trade_count,
        "average_hold_bars": average_hold_bars,
    }


def performance_by_state(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    return (
        trades.groupby("entry_state")
        .agg(
            trade_count=("symbol", "count"),
            win_rate=("pnl", lambda series: (series > 0).mean()),
            average_return=("return_pct", "mean"),
            average_pnl=("pnl", "mean"),
        )
        .sort_values("average_return", ascending=False)
        .reset_index()
    )


def performance_by_transition(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    return (
        trades.groupby("transition_type")
        .agg(
            trade_count=("symbol", "count"),
            win_rate=("pnl", lambda series: (series > 0).mean()),
            average_return=("return_pct", "mean"),
            average_pnl=("pnl", "mean"),
        )
        .sort_values("average_return", ascending=False)
        .reset_index()
    )


def subperiod_performance(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    periods = pd.to_datetime(trades["exit_timestamp"]).dt.to_period("Q").astype(str)
    with_periods = trades.assign(subperiod=periods)
    return (
        with_periods.groupby("subperiod")
        .agg(
            trade_count=("symbol", "count"),
            win_rate=("pnl", lambda series: (series > 0).mean()),
            average_return=("return_pct", "mean"),
            average_pnl=("pnl", "mean"),
        )
        .reset_index()
    )


def _years_in_sample(index: pd.Index, annualization_factor: int) -> float:
    if len(index) <= 1:
        return 0.0
    try:
        delta_days = (pd.Timestamp(index[-1]) - pd.Timestamp(index[0])).days
    except Exception:
        delta_days = 0
    if delta_days > 0:
        return delta_days / 365.25
    return len(index) / annualization_factor
