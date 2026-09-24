from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from trader_engine.execution.crypto_manager import plan_crypto_actions


def snapshot():
    now = datetime(2026, 9, 24, 3, tzinfo=timezone.utc)
    bars = [{"t": (now.replace(hour=0)-timedelta(days=200-i)).isoformat(), "c": "100" if i < 199 else "110"} for i in range(200)]
    return {
        "now": now.isoformat(), "expected_account_id": "paper-id",
        "account": {"id": "paper-id", "status": "ACTIVE", "trading_blocked": False, "account_blocked": False, "cash": "100000", "equity": "100000"},
        "daily_start_equity": "100000", "entries_enabled": True,
        "positions": [], "open_orders": [],
        "bars": {s: deepcopy(bars) for s in ("BTC/USD", "ETH/USD")},
        "quotes": {s: {"t": now.isoformat(), "bp": "109.99", "ap": "110"} for s in ("BTC/USD", "ETH/USD")},
        "assets": {s: {"status": "active", "tradable": True, "min_trade_increment": "0.000000001", "min_order_size": "0.000000001", "price_increment": "0.01"} for s in ("BTC/USD", "ETH/USD")},
    }


def plans(s=None, owned=None, attempted=None):
    return plan_crypto_actions(s or snapshot(), owned or {}, attempted)["plans"]


def held(s, qty="0.146629544", available=None):
    s["positions"] = [{"symbol": "BTCUSD", "qty": qty, "qty_available": available or qty, "market_value": "12500", "side": "long"}]
    return {"BTC/USD": qty}


def bearish(s):
    s["bars"]["BTC/USD"][-1]["c"] = "90"


def test_entries_cap_cash_and_do_not_mutate_input():
    s = snapshot()
    original = deepcopy(s)
    result = plans(s)
    assert [p["action"] for p in result] == ["entry", "entry"]
    costs = [D(p["order"]["qty"])*D(p["order"]["limit_price"]) for p in result]
    assert all(x <= 12400 for x in costs)
    assert sum(costs) <= 25000
    assert s == original
    s["account"]["cash"] = "100"
    result = plans(s)
    total = sum(D(p["order"]["qty"])*D(p["order"]["limit_price"])*D("1.0025") for p in result if p["action"] == "entry")
    assert total <= 100


@pytest.mark.parametrize("problem", ["missing", "duplicate", "future", "intraday", "nan"])
def test_invalid_bars_block_without_lookahead(problem):
    s = snapshot()
    b = s["bars"]["BTC/USD"]
    if problem == "missing": b.pop(0)
    elif problem == "duplicate": b[1] = b[0]
    elif problem == "future": b[-1]["t"] = "2026-09-24T00:00:00Z"
    elif problem == "intraday": b[-1]["t"] = "2026-09-23T01:00:00Z"
    else: b[-1]["c"] = "NaN"
    assert plans(s)[0]["action"] == "blocked"


@pytest.mark.parametrize("quote", [
    {"t": "2026-09-24T02:58:59Z", "ap": 110, "bp": 110},
    {"t": "2026-09-24T03:00:01Z", "ap": 110, "bp": 110},
    {"t": "2026-09-24T03:00:00Z", "ap": 110, "bp": 100},
    {"t": "2026-09-24T03:00:00Z", "ap": 100, "bp": 110},
])
def test_bad_quote_blocks_entry(quote):
    s = snapshot()
    s["quotes"]["BTC/USD"] = quote
    assert plans(s)[0]["action"] == "blocked"


def test_owned_exit_needs_no_quote_and_honors_available_precision():
    s = snapshot()
    owned = held(s, available="0.1466295435")
    bearish(s)
    s["quotes"] = {}
    s["entries_enabled"] = False
    s["daily_start_equity"] = "200000"
    p = plans(s, owned)[0]
    assert p["action"] == "exit"
    assert p["order"] == {"symbol": "BTC/USD", "qty": "0.146629543", "side": "sell", "type": "market", "time_in_force": "ioc"}


def test_equality_exits_and_drift_does_not_trigger_rebalance():
    s = snapshot()
    owned = held(s)
    for b in s["bars"]["BTC/USD"]: b["c"] = "100"
    assert plans(s, owned)[0]["action"] == "exit"
    s["bars"]["BTC/USD"][-1]["c"] = "110"
    s["positions"][0]["market_value"] = "40000"
    p = plans(s, owned)
    assert p[0]["action"] == "hold"
    assert p[1]["action"] == "blocked"


def test_ownership_mismatch_blocks_both_exit_and_new_entries():
    s = snapshot()
    held(s)
    bearish(s)
    assert [p["action"] for p in plans(s, {"BTC/USD": "0.15"})] == ["blocked", "blocked"]


def test_pending_order_blocks_exit_and_all_entries():
    s = snapshot()
    owned = held(s)
    bearish(s)
    s["open_orders"] = [{"symbol": "BTCUSD", "side": "sell"}]
    assert [p["action"] for p in plans(s, owned)] == ["blocked", "blocked"]


def test_entry_attempt_not_retried_and_halt_does_not_block_exit():
    s = snapshot()
    p = plans(s, attempted={"2026-09-23": ["BTC/USD"]})
    assert p[0]["action"] == "blocked"
    assert p[1]["action"] == "entry"
    owned = held(s)
    bearish(s)
    s["account"]["equity"] = "97000"
    p = plans(s, owned)
    assert p[0]["action"] == "exit"
    assert p[1]["action"] == "blocked"
    assert "daily loss halt" in p[1]["reason"]


@pytest.mark.parametrize("field,value", [("id", "wrong"), ("trading_blocked", True), ("equity", "NaN"), ("cash", "-1")])
def test_invalid_account_blocks_everything(field, value):
    s = snapshot()
    owned = held(s)
    bearish(s)
    s["account"][field] = value
    assert all(p["action"] == "blocked" for p in plans(s, owned))


def test_conflicting_ownership_aliases_fail_closed():
    s = snapshot()
    held(s, "2")
    bearish(s)
    assert all(p["action"] == "blocked" for p in plans(s, {"BTCUSD": "1", "BTC/USD": "2"}))


@pytest.mark.parametrize("which", ["snapshot", "ownership", "attempted"])
def test_invalid_mapping_types_fail_closed(which):
    args = {"snapshot": snapshot(), "ownership": {}, "attempted": {}}
    args[which] = []
    assert all(p["action"] == "blocked" for p in plan_crypto_actions(**args)["plans"])
