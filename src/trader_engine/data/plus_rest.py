"""Bounded GET-only SIP/OPRA ingestion; no entitlement fallback or trading methods.

Transport injection: callable(url, headers, timeout) -> (HTTP status, headers,
JSON payload). A trusted transport must not follow redirects. Default urllib
transport rejects all redirects and forwards credentials only to fixed Alpaca
origins. Failures never include response bodies, headers or exception strings.

Results contain flat records, complete (pagination exhausted, NOT proof of data
quality), errors, pages, missing_symbols, and provenance (endpoint, requested
feed/parameters, receipt time). Partial pages survive explicitly incomplete.
Option assessments are bounded data-quality screens, never trade recommendations.
"""
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
import re
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

DATA = "https://data.alpaca.markets"
PAPER = "https://paper-api.alpaca.markets"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _transport(url, headers, timeout):
    request = Request(url, headers=headers, method="GET")
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            body = response.read(20_000_001)
            if len(body) > 20_000_000:
                raise ValueError("response exceeds bound")
            return response.status, dict(response.headers), json.loads(body)
    except HTTPError as exc:
        # Do not consume or expose arbitrary error bodies, even for redirects.
        try:
            return exc.code, dict(exc.headers), {}
        finally:
            exc.close()


def _timestamp(value):
    text = str(value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return datetime.combine(date.fromisoformat(text), datetime.min.time(), timezone.utc)
    result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp needs timezone")
    return result.astimezone(timezone.utc)


def _symbols(values):
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 100:
        raise ValueError("supply 1 to 100 symbols")
    if any(not isinstance(s, str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", s) for s in values):
        raise ValueError("invalid symbol")
    if len(set(values)) != len(values):
        raise ValueError("duplicate symbols")
    return list(values)


def _date_range(start, end):
    if not start or not end or _timestamp(start) > _timestamp(end):
        raise ValueError("explicit ordered start and end required")


class PlusRESTClient:
    """Per-client max 5 requests/second, 20 pages by default, 2 retries max.

    Construct outside hot loops so pacing spans operations. Shared concurrent
    calls are serialized. No credentials are discovered or written by this class.
    """
    def __init__(self, key, secret, *, transport=None, sleep=time.sleep,
                 monotonic=time.monotonic, max_pages=20, max_retries=2):
        if not isinstance(key, str) or not key or not isinstance(secret, str) or not secret:
            raise ValueError("credentials required")
        if any(c in key+secret for c in "\r\n"):
            raise ValueError("invalid credentials")
        if type(max_pages) is not int or not 1 <= max_pages <= 100 or type(max_retries) is not int or not 0 <= max_retries <= 3:
            raise ValueError("invalid pagination/retry bound")
        self._headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"}
        self._transport = transport or _transport
        self._sleep, self._clock = sleep, monotonic
        self._pages, self._retries = max_pages, max_retries
        self._last_request = None
        self._lock = threading.Lock()

    def _get(self, endpoint, params):
        parts = urlsplit(endpoint)
        valid = ((parts.netloc == "data.alpaca.markets" and parts.path in ("/v2/stocks/bars", "/v2/stocks/quotes/latest")) or
                 (parts.netloc == "data.alpaca.markets" and re.fullmatch(r"/v1beta1/options/snapshots/[A-Z][A-Z0-9.\-]{0,14}", parts.path)) or
                 (parts.netloc == "paper-api.alpaca.markets" and parts.path == "/v2/options/contracts"))
        if not valid or parts.scheme != "https" or parts.query or parts.fragment or parts.username or parts.password:
            return None, "endpoint_not_allowed"
        url = endpoint + "?" + urlencode(params)
        with self._lock:
            for attempt in range(self._retries+1):
                if self._last_request is not None:
                    self._sleep(max(0, .2-(self._clock()-self._last_request)))
                self._last_request = self._clock()
                try:
                    status, headers, payload = self._transport(url, dict(self._headers), 20)
                except Exception:
                    return None, "transport_failure"
                if status == 200:
                    return (payload, None) if isinstance(payload, dict) else (None, "invalid_json_schema")
                if status not in (429, 500, 502, 503, 504) or attempt == self._retries:
                    return None, f"http_{status}" if type(status) is int else "invalid_http_status"
                delay = min(8, 2**attempt)
                retry_after = next((v for k, v in headers.items() if k.lower() == "retry-after"), None) if isinstance(headers, dict) else None
                if retry_after is not None:
                    try:
                        requested = float(retry_after)
                    except (ValueError, TypeError):
                        try:
                            requested = (parsedate_to_datetime(str(retry_after))-datetime.now(timezone.utc)).total_seconds()
                        except (ValueError, TypeError):
                            return None, "invalid_retry_after"
                    if not math.isfinite(requested) or requested > 30:
                        return None, "retry_after_exceeds_bound"
                    delay = max(delay, requested, 0)
                self._sleep(delay)
        return None, "retry_exhausted"

    def _fetch(self, endpoint, params, field, shape, symbols=None):
        result = {"records": [], "complete": False, "errors": [], "pages": 0, "missing_symbols": [],
                  "provenance": {"endpoint": endpoint, "feed": params.get("feed"), "params": dict(params), "received_at": None}}
        seen_tokens, seen_records = set(), set()
        page_params = dict(params)
        for _ in range(self._pages):
            payload, error = self._get(endpoint, page_params)
            result["provenance"]["received_at"] = datetime.now(timezone.utc).isoformat()
            if error:
                result["errors"].append(error)
                break
            data = payload.get(field)
            if not isinstance(data, dict if shape != "list" else list):
                result["errors"].append("invalid_"+field+"_schema")
                break
            page_records = []
            try:
                if shape == "list":
                    if any(not isinstance(row, dict) or not isinstance(row.get("symbol"), str) for row in data):
                        raise ValueError()
                    page_records = [dict(row) for row in data]
                else:
                    for symbol, value in data.items():
                        values = value if shape == "bars" else [value]
                        if not isinstance(symbol, str) or not isinstance(values, list) or any(not isinstance(row, dict) for row in values):
                            raise ValueError()
                        page_records.extend(dict(row, symbol=symbol) for row in values)
                for row in page_records:
                    identity = (row["symbol"], row.get("t")) if shape == "bars" else row["symbol"]
                    if identity in seen_records:
                        raise ValueError()
                    seen_records.add(identity)
            except (TypeError, ValueError):
                result["errors"].append("invalid_or_duplicate_records")
                break
            result["records"].extend(page_records)
            result["pages"] += 1
            token = payload.get("next_page_token")
            if token in (None, ""):
                result["complete"] = True
                break
            if not isinstance(token, str) or token in seen_tokens or len(token) > 8192:
                result["errors"].append("invalid_or_repeated_page_token")
                break
            seen_tokens.add(token)
            page_params["page_token"] = token
        else:
            result["errors"].append("pagination_limit")
        if symbols:
            found = {row["symbol"] for row in result["records"]}
            result["missing_symbols"] = sorted(set(symbols)-found)
        return result

    def bars(self, symbols, start, end, timeframe="1Day", *, adjustment, asof="-"):
        symbols = _symbols(symbols)
        _date_range(start, end)
        if adjustment not in ("raw", "split", "dividend", "spin-off", "all"):
            raise ValueError("explicit supported adjustment required")
        if not re.fullmatch(r"(?:[1-9]|[1-5][0-9])Min|(?:[1-9]|1[0-9]|2[0-3])Hour|1Day|1Week|(?:1|2|3|4|6|12)Month", timeframe):
            raise ValueError("invalid timeframe")
        if asof != "-":
            date.fromisoformat(asof)
        params = {"symbols": ",".join(symbols), "start": str(start), "end": str(end), "timeframe": timeframe, "adjustment": adjustment, "asof": asof, "feed": "sip", "sort": "asc", "limit": 10000}
        return self._fetch(DATA+"/v2/stocks/bars", params, "bars", "bars", symbols)

    def latest_quotes(self, symbols):
        symbols = _symbols(symbols)
        return self._fetch(DATA+"/v2/stocks/quotes/latest", {"symbols": ",".join(symbols), "feed": "sip"}, "quotes", "map", symbols)

    def option_chain(self, underlying, *, expiration_start=None, expiration_end=None):
        _symbols([underlying])
        params = {"feed": "opra", "limit": 1000}
        if expiration_start is not None or expiration_end is not None:
            _date_range(expiration_start, expiration_end)
            date.fromisoformat(expiration_start); date.fromisoformat(expiration_end)
            params.update(expiration_date_gte=expiration_start, expiration_date_lte=expiration_end)
        return self._fetch(DATA+"/v1beta1/options/snapshots/"+underlying, params, "snapshots", "map")

    def option_contracts(self, underlying, expiration_start, expiration_end):
        _symbols([underlying]); _date_range(expiration_start, expiration_end)
        date.fromisoformat(expiration_start); date.fromisoformat(expiration_end)
        params = {"underlying_symbols": underlying, "expiration_date_gte": expiration_start, "expiration_date_lte": expiration_end, "status": "active", "show_deliverables": "true", "limit": 1000}
        return self._fetch(PAPER+"/v2/options/contracts", params, "option_contracts", "list")


def assess_option_candidates(chain_result, now, contracts=None, max_candidates=100):
    """Quality screen only; finite Greeks do not demonstrate value or profitability.

    At most 1000 snapshots in lexical symbol order. A fresh positive two-sided
    quote, >=1 displayed contracts each side and <=10% midpoint spread pass this
    deliberately broad liquidity screen. IV/Greeks and contract metadata remain
    independent requirements. No execution eligibility or trading signal emitted.
    """
    now = _timestamp(now)
    if type(max_candidates) is not int or not 1 <= max_candidates <= 1000:
        raise ValueError("max_candidates must be 1..1000")
    rows = chain_result.get("records", [])
    if not isinstance(rows, list):
        raise ValueError("records must be list")
    metadata = {row.get("symbol"): row for row in (contracts or {}).get("records", []) if isinstance(row, dict)}
    result = []
    def number(value):
        try:
            value = float(value)
            return value if math.isfinite(value) else None
        except (ValueError, TypeError):
            return None
    for snapshot in sorted((s for s in rows if isinstance(s, dict) and isinstance(s.get("symbol"), str)), key=lambda row: row["symbol"])[:max_candidates]:
        missing, reasons = [], []
        symbol = snapshot["symbol"]
        quote = snapshot.get("latestQuote") or {}
        quote = quote if isinstance(quote, dict) else {}
        bid, ask, bs, az = (number(quote.get(k)) for k in ("bp", "ap", "bs", "as"))
        try:
            age = (now-_timestamp(quote.get("t"))).total_seconds()
        except (ValueError, TypeError):
            age = None
        if age is None: missing.append("quote_timestamp")
        elif not 0 <= age <= 60: reasons.append("stale_or_future_quote")
        spread = None
        if bid is None or ask is None: missing.append("bid_ask")
        elif not 0 < bid <= ask: reasons.append("nonpositive_or_crossed_quote")
        else:
            spread = (ask-bid)/((ask+bid)/2)*100
            if spread > 10: reasons.append("wide_spread")
        if bs is None or az is None: missing.append("displayed_sizes")
        elif min(bs, az) < 1: reasons.append("insufficient_displayed_size")
        greeks = snapshot.get("greeks") or {}
        greeks = greeks if isinstance(greeks, dict) else {}
        for name in ("delta", "gamma", "theta", "vega", "rho"):
            value = number(greeks.get(name))
            if value is None: missing.append("greek_"+name)
            elif (name == "delta" and not -1 <= value <= 1) or (name in ("gamma", "vega") and value < 0):
                reasons.append("invalid_greek_"+name)
        iv = number(snapshot.get("impliedVolatility"))
        if iv is None: missing.append("implied_volatility")
        elif iv <= 0: reasons.append("invalid_implied_volatility")
        contract = metadata.get(symbol, {})
        for name in ("expiration_date", "strike_price", "size", "type", "style"):
            if contract.get(name) in (None, ""): missing.append("contract_"+name)
        if contract:
            try:
                expiry = date.fromisoformat(str(contract.get("expiration_date")))
                if expiry < now.date(): reasons.append("expired_contract")
            except ValueError:
                reasons.append("invalid_contract_expiration")
            strike, size = number(contract.get("strike_price")), number(contract.get("size"))
            if strike is None or strike <= 0: reasons.append("invalid_contract_strike")
            if size is None or size <= 0 or not size.is_integer(): reasons.append("invalid_contract_size")
            if contract.get("type") not in ("call", "put"): reasons.append("invalid_contract_type")
            if contract.get("style") not in ("american", "european"): reasons.append("invalid_contract_style")
        provenance = chain_result.get("provenance", {})
        if not isinstance(provenance, dict) or provenance.get("feed") != "opra":
            reasons.append("opra_provenance_required")
        if not chain_result.get("complete"): reasons.append("incomplete_chain")
        if contracts is None or not contracts.get("complete"): reasons.append("incomplete_contract_metadata")
        result.append({"symbol": symbol, "quality_status": "passes_quality_screen" if not missing and not reasons else "insufficient_or_invalid_evidence", "missing": missing, "reasons": reasons, "quote_asof": quote.get("t"), "quote_age_seconds": age, "spread_pct": spread, "bid": bid, "ask": ask, "implied_volatility": iv, "greeks": greeks, "contract": contract, "execution_eligible": False, "profitable_edge_established": False})
    return {"records": result, "assessed_count": len(result), "input_count": len(rows), "truncated": len(rows)>max_candidates, "purpose": "data_quality_only", "asof": now.isoformat()}
