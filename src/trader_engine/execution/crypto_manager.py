"""Pure plans for the limited BTC/ETH paper experiment; never submits orders.

``plan_crypto_actions(snapshot, ownership, attempted=None)`` accepts JSON data.
Snapshot: now (UTC ISO timestamp), expected_account_id, account (id, status,
trading_blocked, account_blocked, equity, cash), positions (symbol, qty,
qty_available, market_value, side), open_orders, bars and quotes keyed by symbol,
daily_start_equity, entries_enabled (explicit bool), and assets keyed by symbol
(tradable, status, min_trade_increment,
min_order_size, price_increment). Bars must contain exactly 200 consecutive
UTC-midnight {t,c} observations ending yesterday. Quotes use {t,ap,bp}.
Ownership maps symbols to net quantity strings, whose provenance the caller
must independently reconcile. attempted maps signal dates to symbol lists;
record attempts durably BEFORE submission, including unfilled/ambiguous attempts.

Returns {as_of, signal_day, plans}, each with symbol/action/reason and optionally
an order dictionary. This does not establish execution authorization, ownership
provenance, persistence, supervision, or broker freshness. Caller must obtain one
fresh coherent broker snapshot and reconcile pending actions before each call.
Plans are in symbol order: execute exits first, then refresh and replan before
any entry; never treat this output as an atomic multi-order transaction.
Entries use 12.5% equity capped at $12,400 per symbol, 25% total gross exposure,
and 25bps cash reserve. Exits use the tested close <= SMA200 condition, do not
require a quote, and are independent of entry exposure restrictions. No automatic
re-entry retry on a signal day. No same-day stop or promise of loss containment.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR

SYMBOLS = ("BTC/USD", "ETH/USD")
D = Decimal


def _number(value, positive=False):
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("invalid numeric field") from None
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        raise ValueError("invalid numeric field")
    return result


def _time(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() != timedelta(0):
        raise ValueError("UTC timestamp required")
    return result.astimezone(timezone.utc)


def _symbol(value):
    return {"BTCUSD": "BTC/USD", "ETHUSD": "ETH/USD"}.get(value, value)


def _rounded(value, increment, rounding):
    return (value / increment).to_integral_value(rounding=rounding) * increment


def _signal(rows, today):
    if not isinstance(rows, list) or len(rows) != 200:
        raise ValueError("exactly 200 completed daily bars required")
    closes = []
    for offset, row in enumerate(rows):
        stamp = _time(row["t"])
        expected = datetime.combine(today - timedelta(days=200-offset), datetime.min.time(), timezone.utc)
        if stamp != expected:
            raise ValueError("daily bars must be consecutive UTC days ending yesterday")
        closes.append(_number(row["c"], positive=True))
    return closes[-1] > sum(closes) / D(200)


def plan_crypto_actions(snapshot: dict, ownership: dict, attempted: dict | None = None) -> dict:
    """Return deterministic, conservative plans; malformed inputs fail closed."""
    output = {"as_of": snapshot.get("now") if isinstance(snapshot, dict) else None, "signal_day": None, "plans": []}

    def blocked_all(reason):
        output["plans"] = [{"symbol": s, "action": "blocked", "reason": reason} for s in SYMBOLS]
        return output

    if not isinstance(snapshot, dict) or not isinstance(ownership, dict) or (attempted is not None and not isinstance(attempted, dict)):
        return blocked_all("invalid planner input mappings")
    try:
        now = _time(snapshot["now"])
        signal_day = str(now.date() - timedelta(days=1))
        output["signal_day"] = signal_day
        account = snapshot["account"]
        expected_id = snapshot["expected_account_id"]
        if not expected_id or account["id"] != expected_id:
            return blocked_all("account identity mismatch")
        if account["status"] != "ACTIVE" or account["trading_blocked"] is not False or account["account_blocked"] is not False:
            return blocked_all("account blocks trading")
        equity = _number(account["equity"], positive=True)
        cash = _number(account["cash"])
        positions, orders = snapshot["positions"], snapshot["open_orders"]
        if not isinstance(positions, list) or not isinstance(orders, list):
            raise ValueError("complete positions and open orders required")
        normalized = {_symbol(s): q for s, q in ownership.items()}
        if len(normalized) != len(ownership):
            raise ValueError("duplicate ownership aliases")
        ownership = normalized
        held = {}
        gross = D(0)
        foreign = False
        for position in positions:
            symbol = _symbol(position["symbol"])
            if symbol in held:
                raise ValueError("duplicate broker position")
            qty = _number(position["qty"], positive=True)
            available = _number(position["qty_available"])
            value = _number(position["market_value"], positive=True)
            if available > qty or position["side"] != "long":
                raise ValueError("invalid long position")
            held[symbol] = (qty, available)
            gross += value
            if symbol not in SYMBOLS or symbol not in ownership or _number(ownership[symbol]) != qty:
                foreign = True
        owned = {_symbol(s): _number(q) for s, q in ownership.items()}
        if len(owned) != len(ownership) or any(s not in SYMBOLS for s in owned):
            raise ValueError("invalid ownership mapping")
        for symbol, qty in owned.items():
            if qty != held.get(symbol, (D(0), D(0)))[0]:
                foreign = True
        order_symbols = {_symbol(o["symbol"]) for o in orders}
        attempts = (attempted or {}).get(signal_day, [])
        if not isinstance(attempts, list):
            raise ValueError("invalid prior attempts")
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return blocked_all("invalid or incomplete broker snapshot")

    for symbol in SYMBOLS:
        plan = {"symbol": symbol, "action": "blocked", "reason": ""}
        output["plans"].append(plan)
        try:
            qty, available = held.get(symbol, (D(0), D(0)))
            if qty != owned.get(symbol, D(0)):
                raise ValueError("ownership does not match broker quantity")
            if symbol in order_symbols:
                raise ValueError("symbol has an open order; reconcile first")
            bullish = _signal(snapshot["bars"][symbol], now.date())
            if qty and bullish:
                plan.update(action="hold", reason="completed close above SMA200")
                continue
            if not qty and not bullish:
                plan.update(action="hold", reason="flat; completed close at or below SMA200")
                continue
            asset = snapshot["assets"][symbol]
            if asset["tradable"] is not True or asset["status"] != "active":
                raise ValueError("asset is not tradable")
            increment = _number(asset["min_trade_increment"], positive=True)
            minimum = _number(asset["min_order_size"], positive=True)
            if qty:
                sell_qty = _rounded(available, increment, ROUND_FLOOR)
                if sell_qty < minimum:
                    raise ValueError("available owned quantity below order minimum")
                plan.update(action="exit", reason="completed close at or below SMA200", order={"symbol": symbol, "side": "sell", "type": "market", "time_in_force": "ioc", "qty": str(sell_qty)})
                continue
            if snapshot.get("entries_enabled") is not True:
                raise ValueError("new entries disabled")
            daily_start = _number(snapshot["daily_start_equity"], positive=True)
            if equity <= daily_start * D("0.97"):
                raise ValueError("3 percent daily loss halt blocks new entries")
            if foreign or orders:
                raise ValueError("unowned positions or open orders block new entries")
            if symbol in attempts:
                raise ValueError("entry already attempted for this signal day")
            quote = snapshot["quotes"][symbol]
            age = (now - _time(quote["t"])).total_seconds()
            if not 0 <= age <= 60:
                raise ValueError("quote must be fresh within 60 seconds and not future dated")
            ask, bid = _number(quote["ap"], True), _number(quote["bp"], True)
            if ask < bid or (ask-bid)/((ask+bid)/2) > D("0.0025"):
                raise ValueError("crossed quote or spread above 25bps")
            tick = _number(asset["price_increment"], positive=True)
            limit = _rounded(ask * D("1.0005"), tick, ROUND_CEILING)
            budget = min(equity*D("0.125"), D(12400), max(D(0), equity*D("0.25")-gross), cash/D("1.0025"))
            buy_qty = _rounded(budget/limit, increment, ROUND_FLOOR)
            if buy_qty < minimum:
                raise ValueError("insufficient cash or exposure allowance for minimum order")
            notional = buy_qty*limit
            cash -= notional*D("1.0025")
            gross += notional
            plan.update(action="entry", reason="completed close above SMA200", order={"symbol": symbol, "side": "buy", "type": "limit", "time_in_force": "ioc", "qty": str(buy_qty), "limit_price": str(limit)})
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            plan["reason"] = str(exc) if isinstance(exc, ValueError) else "missing or invalid symbol evidence"
    return output
