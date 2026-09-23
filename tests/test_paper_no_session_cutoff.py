"""Disabling PAPER session P&L limits leaves independent entry limits intact."""
from dataclasses import fields
from decimal import Decimal

import pytest

from trader_engine.execution.sizing import PaperSizingConfig, size_long_entry


def size(config=None, **overrides):
    account = dict(equity="95000", cash="95000", session_start_equity="100000",
                   entry_price="100", stop_price="99")
    account.update(overrides)
    return size_long_entry(config or PaperSizingConfig(max_session_loss=None), **account)


def test_explicitly_disabled_session_loss_allows_entries_after_large_loss():
    result = size()
    assert result.allowed
    assert result.quantity == 75
    assert result.risk_budget == Decimal("237.5")
    assert result.rejection_reason is None


def test_default_session_cutoff_still_blocks_large_loss():
    config = PaperSizingConfig()
    assert config.max_session_loss == Decimal("1000")
    result = size(config)
    assert not result.allowed
    assert result.rejection_reason == "session loss cutoff reached"


def test_disabled_session_budget_does_not_reserve_risk_against_absent_budget():
    result = size(open_stop_risk="10000", reserved_stop_risk="1000", stop_price="80")
    assert result.quantity == 11
    assert result.risk_budget == Decimal("237.5")
    assert result.modeled_stop_loss <= result.risk_budget
    assert "stop_risk" in result.limiting_factors


@pytest.mark.parametrize("loss_limit", [0, -1, True, "NaN", "Infinity", "-Infinity", "none", ""])
def test_invalid_session_loss_values_still_rejected(loss_limit):
    with pytest.raises(ValueError, match="max_session_loss"):
        PaperSizingConfig(max_session_loss=loss_limit)


@pytest.mark.parametrize("name", [f.name for f in fields(PaperSizingConfig) if f.name != "max_session_loss"])
def test_none_is_not_allowed_for_other_config_fields(name):
    with pytest.raises(ValueError, match=name):
        PaperSizingConfig(**{name: None})


@pytest.mark.parametrize("overrides,quantity,limiting", [
    ({"cash": "0"}, 0, "cash"),
    ({"cash": "200", "reserved_entry_notional": "100", "reserved_entry_cash": "100"}, 0, "cash"),
    ({"cash": "1000"}, 9, "cash"),
    ({"equity": "10000", "cash": "400000"}, 10, "position_cap"),
    ({"gross_open_notional": "47500"}, 0, "gross_cap"),
    ({"gross_open_notional": "45000", "reserved_entry_notional": "2400"}, 1, "gross_cap"),
    ({"gross_open_notional": "9500", "symbol_open_notional": "9500"}, 0, "position_cap"),
])
def test_disabled_session_cutoff_retains_independent_cash_equity_and_gross_caps(overrides, quantity, limiting):
    result = size(**overrides)
    assert result.quantity == quantity
    assert limiting in result.limiting_factors
    assert result.rejection_reason != "session loss cutoff reached"


@pytest.mark.parametrize("equity", [None, 0, -1, "NaN", "Infinity"])
def test_disabling_session_cutoff_does_not_bypass_actual_equity_validation(equity):
    with pytest.raises(ValueError, match="equity"):
        size(equity=equity)
