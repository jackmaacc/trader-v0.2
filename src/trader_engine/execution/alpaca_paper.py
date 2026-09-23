"""Small paper-only broker adapter. No mutation is enabled by default."""
from __future__ import annotations

from collections import deque
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from http.client import HTTPException
import json
import math
import os
import re
import threading
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


class BrokerError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class EntryNotSubmitted(BrokerError):
    """The entry was blocked locally before any HTTP submission."""
    pass


class RollingRateLimiter:
    """Share across clients: reserve the final 30 requests/minute for recovery."""

    def __init__(self, *, clock: Callable = time.monotonic, sleep: Callable = time.sleep, max_queue=256):
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._requests = deque()
        self._last = float('-inf')
        self._blocked_until = 0.0
        if max_queue < 1:
            raise ValueError("Queue must be bounded and positive")
        self.max_queue = max_queue
        self._pending = {}
        self._sequence = 0

    PRIORITIES = {'emergency': 0, 'monitor': 1, 'entry': 2, 'discovery': 3}

    def enqueue(self, priority='monitor'):
        if priority not in self.PRIORITIES:
            raise ValueError('Unknown request priority')
        with self._lock:
            normal_queue_cap = max(1, self.max_queue - min(8, self.max_queue // 4))
            if len(self._pending) >= self.max_queue or (priority != 'emergency' and len(self._pending) >= normal_queue_cap):
                raise BrokerError('Request queue full; admission refused')
            self._sequence += 1
            ticket = self._sequence
            self._pending[ticket] = priority
            return ticket

    def cancel(self, ticket):
        with self._lock:
            self._pending.pop(ticket, None)

    def try_admit(self, ticket):
        """Return (admitted, retry_seconds); deterministic and safe for event loops."""
        with self._lock:
            if ticket not in self._pending:
                raise ValueError('Unknown queue ticket')
            now = self._clock()
            while self._requests and self._requests[0] <= now - 60:
                self._requests.popleft()
            head = min(self._pending, key=lambda t: (self.PRIORITIES[self._pending[t]], t))
            if head != ticket:
                return False, .01
            priority = self._pending[ticket]
            capacity = 180 if priority == 'emergency' else 150
            wait = max(0., self._last + .36 - now, self._blocked_until - now)
            if len(self._requests) >= capacity:
                wait = max(wait, self._requests[len(self._requests)-capacity]+60-now)
            if wait > 0:
                return False, wait
            del self._pending[ticket]
            self._requests.append(now)
            self._last = now
            return True, 0.

    def acquire(self, *, entry: bool = False, priority=None) -> None:
        # Legacy callers retain their explicit recovery classification.
        ticket = self.enqueue(priority or ('entry' if entry else 'emergency'))
        try:
            while True:
                admitted, wait = self.try_admit(ticket)
                if admitted:
                    return
                self._sleep(wait)
        finally:
            self.cancel(ticket)

    def throttle(self, seconds: float = 60.0) -> None:
        if not math.isfinite(seconds) or seconds <= 0:
            seconds = 60.0
        with self._lock:
            self._blocked_until = max(self._blocked_until, self._clock() + seconds)


_SHARED_LIMITER = RollingRateLimiter()
_MISSING = object()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.\-]{1,128}', value):
        raise ValueError('Invalid broker identifier')
    return quote(value, safe='')


def _decimal(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise BrokerError('Broker returned an invalid quantity') from None
    if not result.is_finite():
        raise BrokerError('Broker returned a nonfinite quantity')
    return result


class AlpacaPaperClient:
    def __init__(self, api_key=None, secret=None, telemetry=None, allow_orders=False, limiter=None, entry_guard=None):
        self._api_key = api_key if api_key is not None else os.environ.get('APCA_API_KEY_ID')
        self._secret = secret if secret is not None else os.environ.get('APCA_API_SECRET_KEY')
        if not self._api_key or not self._secret:
            raise ValueError('Paper broker credentials are missing')
        if any(not isinstance(value, str) or not value.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in value) for value in (self._api_key, self._secret)):
            raise ValueError('Paper broker credential format is invalid')
        self.allow_orders = allow_orders is True
        self.telemetry = telemetry
        self.entry_guard = entry_guard
        self.telemetry_failed = False
        self.limiter = limiter if limiter is not None else _SHARED_LIMITER
        self._opener = build_opener(_NoRedirect())
        self._closed = False

    def __enter__(self):
        if self._closed:
            raise BrokerError('Broker client is closed')
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self._closed = True
        self._opener.close()
        self._api_key = self._secret = None

    def _emit(self, method, path, status):
        if self.telemetry is not None:
            try:
                self.telemetry({'method': method, 'path': path, 'status': status})
            except BaseException:
                # No callback failure may hide an accepted broker response.
                self.telemetry_failed = True

    @staticmethod
    def _retry_delay(headers):
        delays = [1.0]
        value = headers.get('Retry-After')
        if value:
            try:
                delays.append(float(value))
            except (ValueError, TypeError):
                try:
                    delays.append(parsedate_to_datetime(value).timestamp() - time.time())
                except (ValueError, TypeError, OverflowError):
                    pass
        reset = headers.get('X-RateLimit-Reset')
        if reset:
            try:
                delays.append(float(reset) - time.time())
            except (ValueError, TypeError):
                pass
        finite = [v for v in delays if math.isfinite(v) and v > 0]
        return max(finite) if len(finite) > 1 else 60.0

    def _request(self, method, path, *, payload=None, params=None, entry=False, missing=False, priority=None):
        if self._closed:
            raise BrokerError('Broker client is closed')
        if method not in ('GET', 'POST', 'DELETE'):
            raise ValueError('Unsupported paper broker method')
        if method != 'GET' and not self.allow_orders:
            raise BrokerError('Order mutations are disabled')
        # This method is not a general URL fetcher: credentials have one fixed origin.
        if not path.startswith('/v2/') or '?' in path or '#' in path or '..' in path:
            raise ValueError('Invalid paper broker path')
        url = 'https://paper-api.alpaca.markets' + path
        if params:
            url += '?' + urlencode(params)
        data = None if payload is None else json.dumps(payload, allow_nan=False).encode('utf-8')
        request = Request(url, data=data, method=method, headers={
            'APCA-API-KEY-ID': self._api_key,
            'APCA-API-SECRET-KEY': self._secret,
            'Content-Type': 'application/json',
        })
        if isinstance(self.limiter, RollingRateLimiter):
            priority = priority or ("entry" if entry else ("emergency" if method != "GET" else "monitor"))
            self.limiter.acquire(entry=entry, priority=priority)
        else:
            self.limiter.acquire(entry=entry)
        if entry and self.entry_guard is not None:
            try:
                permitted = self.entry_guard()
            except Exception:
                raise BrokerError('Entry guard failed before submission') from None
            if not permitted:
                raise EntryNotSubmitted('Entry stopped locally before submission')
        try:
            with self._opener.open(request, timeout=15) as response:
                status = response.status
                content = response.read(2 * 1024 * 1024 + 1)
                if not 200 <= status < 300:
                    raise BrokerError('Paper broker returned an unexpected HTTP status', status=status)
                if len(content) > 2 * 1024 * 1024:
                    raise BrokerError('Paper broker response exceeded its size limit')
                try:
                    result = json.loads(content) if content else None
                except (ValueError, UnicodeError):
                    raise BrokerError('Paper broker returned invalid JSON') from None
        except HTTPError as exc:
            status = exc.code
            if status == 429:
                self.limiter.throttle(self._retry_delay(exc.headers))
            exc.close()
            self._emit(method, path, status)
            if status == 404 and missing:
                return _MISSING
            raise BrokerError('Paper broker request failed with HTTP ' + str(status), status=status) from None
        except (URLError, OSError, TimeoutError, HTTPException):
            self._emit(method, path, None)
            raise BrokerError('Paper broker request failed; broker outcome may be unknown') from None
        self._emit(method, path, status)
        return result

    @staticmethod
    def _object(result):
        if not isinstance(result, dict):
            raise BrokerError('Paper broker returned an unexpected response shape')
        return result

    def get_order(self, client_id, *, priority="monitor"):
        _identifier(client_id)
        value = self._request('GET', '/v2/orders:by_client_order_id',
                              params={'client_order_id': client_id}, missing=True, priority=priority)
        return None if value is _MISSING else self._object(value)

    def submit(self, payload):
        if not self.allow_orders:
            raise BrokerError('Order mutations are disabled')
        if not isinstance(payload, dict) or payload.get('side') not in ('buy', 'sell'):
            raise ValueError('Invalid paper order payload')
        _identifier(payload.get('symbol'))
        _identifier(payload.get('client_order_id'))
        if _decimal(payload.get('qty')) <= 0:
            raise ValueError('Order quantity must be positive')
        # Exactly one POST attempt. The lifecycle owner resolves ambiguous outcomes.
        return self._object(self._request('POST', '/v2/orders', payload=payload,
                                         entry=payload['side'] == 'buy'))

    def cancel(self, order_id):
        if not self.allow_orders:
            raise BrokerError('Order mutations are disabled')
        self._request('DELETE', '/v2/orders/' + _identifier(order_id))

    def position_qty(self, symbol):
        value = self._request('GET', '/v2/positions/' + _identifier(symbol), missing=True)
        if value is _MISSING:
            return Decimal(0)
        value = self._object(value)
        if value.get('symbol') != symbol:
            raise BrokerError('Paper broker position identity mismatch')
        return _decimal(value.get('qty'))

    def open_orders(self):
        value = self._request('GET', '/v2/orders', params={'status': 'open', 'limit': 500, 'nested': 'true'})
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise BrokerError('Paper broker returned invalid open orders')
        if len(value) >= 500:
            raise BrokerError('Open-order response may be truncated; reconciliation is incomplete')
        return value

    def account(self):
        return self._object(self._request('GET', '/v2/account'))

    def clock(self):
        return self._object(self._request('GET', '/v2/clock'))

    def positions(self):
        value = self._request('GET', '/v2/positions')
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise BrokerError('Paper broker returned invalid positions')
        return value
