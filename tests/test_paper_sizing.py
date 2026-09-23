"""Offline sizing checks; importing the staged module cannot contact a broker."""

import importlib.util
from dataclasses import fields, replace
from decimal import Decimal, localcontext, ROUND_UP
from pathlib import Path
import random
import sys

import pytest


_path = Path(__file__).with_name("sizing.py")
if _path.exists():
    _spec = importlib.util.spec_from_file_location("staged_paper_sizing", _path)
    sizing = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = sizing
    _spec.loader.exec_module(sizing)
else:
    from trader_engine.execution import sizing

PaperSizingConfig = sizing.PaperSizingConfig
size_long_entry = sizing.size_long_entry
D = Decimal


def size(config=None, **overrides):
    account = dict(equity="100000", cash="100000", session_start_equity="100000",
                   entry_price="100", stop_price="99")
    account.update(overrides)
    return size_long_entry(config or PaperSizingConfig(), **account)


def test_dormant_profile_sizes_meaningful_whole_share_entry():
    result = size()
    assert result.allowed
    assert result.quantity == 75
    assert result.notional == D("7500")
    assert result.modeled_stop_loss == D("76.50")
    assert result.reserved_cash == D("7501.50")
    assert result.limiting_factors == ("target_notional",)


def test_equity_limits_capacity_even_with_large_cash_balance():
    result = size(equity="50000", session_start_equity="50000", cash="400000")
    assert result.quantity == 50
    assert result.notional == D("5000")
    assert "position_cap" in result.limiting_factors
    with pytest.raises(TypeError):
        size(buying_power="400000")


def test_wider_stop_reduces_size_to_equity_risk_budget():
    result = size(stop_price="80")
    assert result.quantity == 12
    assert result.modeled_stop_loss == D("240.24")
    assert result.risk_budget == D("250")
    assert "stop_risk" in result.limiting_factors


def test_open_positions_and_pending_buys_consume_gross_capacity():
    result = size(gross_open_notional="45000", reserved_entry_notional="4000")
    assert result.quantity == 10
    assert result.notional + D("45000") + D("4000") == D("50000")
    assert "gross_cap" in result.limiting_factors


def test_same_symbol_positions_and_pending_buys_consume_position_capacity():
    result = size(gross_open_notional="7000", symbol_open_notional="7000",
                  reserved_entry_notional="2000", symbol_reserved_notional="2000")
    assert result.quantity == 10
    assert "position_cap" in result.limiting_factors


def test_cash_and_pending_cash_reservations_include_cost_buffer():
    result = size(cash="2001", reserved_entry_notional="1000", reserved_entry_cash="1001")
    assert result.quantity == 9
    assert result.reserved_cash == D("900.18")
    assert result.reserved_cash + D("1001") <= D("2001")
    assert "cash" in result.limiting_factors


@pytest.mark.parametrize("equity", ["99000", "98999.99"])
def test_marked_session_loss_at_or_past_cutoff_blocks_entry(equity):
    result = size(equity=equity)
    assert not result.allowed
    assert result.quantity == 0
    assert result.rejection_reason == "session loss cutoff reached"


def test_outstanding_stop_risk_reserves_remaining_session_budget():
    result = size(equity="99100", open_stop_risk="50", reserved_stop_risk="20")
    assert result.risk_budget == D("30")
    assert result.quantity == 29
    assert result.modeled_stop_loss <= D("30")


def test_risk_reservations_can_exhaust_budget_before_session_cutoff():
    result = size(open_stop_risk="750", reserved_stop_risk="250")
    assert result.quantity == 0
    assert "stop_risk" in result.limiting_factors


@pytest.mark.parametrize("overrides", [
    {"cash": "0"},
    {"gross_open_notional": "50000"},
    {"gross_open_notional": "51000"},
    {"gross_open_notional": "10000", "symbol_open_notional": "10000"},
    {"entry_price": "10001", "stop_price": "10000"},
])
def test_exhausted_limits_do_not_force_a_one_share_order(overrides):
    result = size(**overrides)
    assert not result.allowed
    assert result.quantity == 0
    assert result.notional == result.reserved_cash == result.modeled_stop_loss == 0
    assert result.rejection_reason


@pytest.mark.parametrize("name", [
    "equity", "cash", "session_start_equity", "entry_price", "stop_price",
    "gross_open_notional", "reserved_entry_notional", "symbol_open_notional",
    "symbol_reserved_notional", "open_stop_risk", "reserved_stop_risk", "reserved_entry_cash",
])
@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", -1, True, None, "bad"])
def test_invalid_account_inputs_are_rejected(name, bad):
    # None is explicitly the supported default only for reserved_entry_cash.
    if name == "reserved_entry_cash" and bad is None:
        return
    with pytest.raises(ValueError, match=name):
        size(**{name: bad})


@pytest.mark.parametrize("name", ["equity", "session_start_equity", "entry_price", "stop_price"])
def test_zero_required_positive_inputs_are_rejected(name):
    with pytest.raises(ValueError, match=name):
        size(**{name: "0"})


@pytest.mark.parametrize("overrides", [
    {"stop_price": "100"}, {"stop_price": "101"},
    {"symbol_open_notional": "1"}, {"symbol_reserved_notional": "1"},
    {"reserved_entry_notional": "100", "reserved_entry_cash": "99"},
])
def test_inconsistent_stop_or_reservations_are_rejected(overrides):
    with pytest.raises(ValueError):
        size(**overrides)


@pytest.mark.parametrize("name", [field.name for field in fields(PaperSizingConfig)])
@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-1", False])
def test_invalid_config_is_rejected(name, bad):
    with pytest.raises(ValueError, match=name):
        PaperSizingConfig(**{name: bad})


@pytest.mark.parametrize("overrides", [
    {"target_notional": 0}, {"target_notional": "10001"},
    {"max_position_notional": "60000"},
    {"max_position_equity_fraction": "1.01"},
    {"max_gross_equity_fraction": "1.01"},
    {"risk_per_trade_equity_fraction": "1.01"},
    {"max_position_equity_fraction": ".6"},
])
def test_invalid_profile_relationships_are_rejected(overrides):
    with pytest.raises(ValueError):
        PaperSizingConfig(**overrides)


def test_exact_decimal_cash_boundary_always_rounds_down():
    config = replace(PaperSizingConfig(), per_share_cost_buffer="0")
    with localcontext() as context:
        context.rounding = ROUND_UP
        result = size(config, cash="0.30", entry_price="0.10", stop_price="0.09")
        assert result.quantity == 3
        assert result.notional == D("0.30")
        assert context.rounding == ROUND_UP


def test_config_is_immutable_and_does_not_activate_anything():
    config = PaperSizingConfig()
    with pytest.raises(AttributeError):
        config.target_notional = D("10000")
    assert not hasattr(config, "submit")


def test_varied_accounts_never_exceed_any_independent_limit():
    # Representative uneven prices/reservations exercise flooring and competing
    # constraints together, independent of whichever bound wins each example.
    rng = random.Random(84219)
    config = PaperSizingConfig()
    for _ in range(200):
        equity = D(rng.randint(5000000, 20000000)) / 100
        loss = D(rng.randint(0, 120000)) / 100
        cash = D(rng.randint(0, 20000000)) / 100
        price = D(rng.randint(1, 200000)) / 100
        stop = price * D("0.97")
        gross = D(rng.randint(0, 6000000)) / 100
        reserved = D(rng.randint(0, 1000000)) / 100
        held_symbol = min(gross, D(rng.randint(0, 1000000)) / 100)
        reserved_symbol = min(reserved, D(rng.randint(0, 300000)) / 100)
        open_risk = D(rng.randint(0, 100000)) / 100
        pending_risk = D(rng.randint(0, 10000)) / 100
        result = size(config, equity=equity, cash=cash, session_start_equity=equity+loss,
                      entry_price=price, stop_price=stop, gross_open_notional=gross,
                      reserved_entry_notional=reserved, symbol_open_notional=held_symbol,
                      symbol_reserved_notional=reserved_symbol,
                      open_stop_risk=open_risk, reserved_stop_risk=pending_risk)
        assert isinstance(result.quantity, int) and result.quantity >= 0
        if not result.allowed:
            continue
        assert result.notional <= config.target_notional
        assert result.notional + held_symbol + reserved_symbol <= min(
            config.max_position_notional, equity * config.max_position_equity_fraction)
        assert result.notional + gross + reserved <= min(
            config.max_gross_notional, equity * config.max_gross_equity_fraction)
        assert result.reserved_cash + reserved <= cash
        assert result.modeled_stop_loss <= equity * config.risk_per_trade_equity_fraction
        assert result.modeled_stop_loss + open_risk + pending_risk + loss <= config.max_session_loss
