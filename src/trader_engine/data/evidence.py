"""Immutable, read-only market-data evidence for the fixed ETF research basket.

Completeness is an audit result, never a hindsight eligibility filter. Historical
receipt time is acquisition time, not an invented original publication timestamp.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

import pandas as pd

from trader_engine.data.alpaca_intraday import (
    DataResponse, READ_ONLY_ENDPOINTS, request_data_raw,
)

ETF_UNIVERSE = ("SPY", "QQQ", "IWM", "TLT", "GLD")
KINDS = {"minute", "daily", "quotes", "actions", "calendar"}


def _utc(value):
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        raise ValueError("Explicit timezone required")
    return stamp.tz_convert("UTC")


def _interval(start, end):
    try:
        if not isinstance(start, str) or not isinstance(end, str):
            raise ValueError
        if date.fromisoformat(start).isoformat() != start or date.fromisoformat(end).isoformat() != end:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("Use ISO YYYY-MM-DD session dates") from None
    first, last = pd.Timestamp(start), pd.Timestamp(end)
    if first.tzinfo is not None or last.tzinfo is not None:
        raise ValueError("Use ISO session dates, not timestamps")
    if first != first.normalize() or last != last.normalize() or first > last:
        raise ValueError("Invalid session date interval")
    return first.date().isoformat(), last.date().isoformat()


def parse_calendar(payload, start=None, end=None):
    """Validate provider session hours including DST and early closes."""
    if not isinstance(payload, list):
        raise ValueError("Calendar response must be a list")
    rows, seen = [], set()
    for row in payload:
        day = str(row["date"])
        if day in seen:
            raise ValueError("Duplicate calendar session")
        if (start and day < start) or (end and day > end):
            raise ValueError("Calendar outside requested interval")
        seen.add(day)
        opening = pd.Timestamp(day + " " + row["open"], tz="America/New_York").tz_convert("UTC")
        closing = pd.Timestamp(day + " " + row["close"], tz="America/New_York").tz_convert("UTC")
        if opening >= closing or (closing - opening).total_seconds() % 60:
            raise ValueError("Invalid calendar session hours")
        rows.append({"date": day, "open": opening.isoformat(), "close": closing.isoformat()})
    return sorted(rows, key=lambda row: row["date"])


def causal_bar_window(frame, decision_at, lookback):
    """Only past completed minute bars can determine decision eligibility."""
    if lookback < 1:
        raise ValueError("lookback must be positive")
    decision = _utc(decision_at)
    expected = pd.date_range(end=decision.floor("min") - pd.Timedelta(minutes=1),
                             periods=lookback, freq="min")
    if frame.index.has_duplicates:
        raise ValueError("Duplicate bar timestamps")
    window = frame.reindex(expected)
    required = ["open", "high", "low", "close", "volume"]
    eligible = all(column in window for column in required)
    eligible = eligible and not window[required].isna().any().any()
    return window, bool(eligible)


def coverage_audit(frames, calendar, *, timeframe="1Min"):
    """Retain all observed bars; report missing sessions/minutes without deletion."""
    records = []
    for symbol, frame in frames.items():
        timestamps = pd.DatetimeIndex(frame.index)
        if timestamps.tz is None:
            raise ValueError("Bar timestamps require timezone")
        timestamps = timestamps.tz_convert("UTC")
        if timestamps.has_duplicates:
            raise ValueError("Duplicate bar timestamps")
        for session in calendar:
            opening, closing = _utc(session["open"]), _utc(session["close"])
            if timeframe == "1Min":
                expected = pd.date_range(opening, closing, freq="min", inclusive="left")
                observed = timestamps[(timestamps >= opening) & (timestamps < closing)]
                missing = expected.difference(observed)
                extra = observed.difference(expected)
                missing_values = [value.isoformat() for value in missing]
            elif timeframe == "1Day":
                days = timestamps.tz_convert("America/New_York").strftime("%Y-%m-%d")
                observed = timestamps[days == session["date"]]
                expected = [session["date"]]
                missing_values = [] if len(observed) else [session["date"]]
                extra = observed[1:]
            else:
                raise ValueError("Unsupported audit timeframe")
            records.append({
                "symbol": symbol, "date": session["date"],
                "expected_count": len(expected), "observed_count": len(observed),
                "missing": missing_values, "unexpected_count": len(extra),
                "complete": not missing_values and not len(extra),
                "eligibility_policy": "past_completed_window_only",
                "whole_session_deleted": False,
            })
    return records


class EvidenceRecorder:
    """Archive exact response bytes and nonsecret request metadata before parsing."""
    def __init__(self, output, fetcher: Callable | None = None):
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=False)
        (self.output / "raw").mkdir()
        self.fetcher = fetcher or request_data_raw
        self.pages = 0

    def request(self, kind, params):
        if (self.output / "manifest.json").exists():
            raise ValueError("Evidence archive is finalized")
        if kind not in READ_ONLY_ENDPOINTS:
            raise ValueError("Endpoint is not allowlisted")
        try:
            result = self.fetcher(kind, dict(params))
        except Exception as exc:
            # Retain request lineage without serializing arbitrary exception text or credentials.
            record = {"method": "GET", "endpoint": READ_ONLY_ENDPOINTS[kind],
                      "kind": kind, "params": dict(params),
                      "received_at": datetime.now(timezone.utc).isoformat(),
                      "failure_type": type(exc).__name__,
                      "status": getattr(exc, "status_code", None), "response_file": None}
            with (self.output / "lineage.jsonl").open("a") as stream:
                stream.write(json.dumps(record, sort_keys=True) + "\n")
            raise
        if isinstance(result, DataResponse):
            body, received, status = result.raw, result.received_at, result.status
            payload = json.loads(body)
            encoding = "exact_http_response_bytes"
            if payload != result.payload:
                raise ValueError("Raw and decoded response disagree")
        else:
            payload = result
            body = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            received = datetime.now(timezone.utc).isoformat()
            status, encoding = 200, "reserialized_injected_fetcher_payload"
        _utc(received)
        relative = f"raw/page-{self.pages:06d}.json"
        (self.output / relative).write_bytes(body)
        record = {
            "page": self.pages, "method": "GET", "endpoint": READ_ONLY_ENDPOINTS[kind],
            "kind": kind, "params": dict(params), "received_at": received,
            "status": status, "response_file": relative, "response_bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(), "representation": encoding,
        }
        with (self.output / "lineage.jsonl").open("a") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
        self.pages += 1
        return payload

    def finish(self, metadata, complete):
        manifest = dict(metadata, complete=complete, page_count=self.pages,
                        publication_time_known=False,
                        hashes={str(path.relative_to(self.output)): hashlib.sha256(path.read_bytes()).hexdigest()
                                for path in sorted(self.output.rglob("*"))
                                if path.is_file() and path.name != "manifest.json"})
        encoded = json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False)
        with (self.output / "manifest.json").open('x') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        return manifest


def verify_archive(output):
    """Verify retained bytes, not market correctness or original arrival times."""
    root = Path(output)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Evidence root must be a real directory')
    manifest_path = root / 'manifest.json'
    if manifest_path.is_symlink():
        raise ValueError('Symlink manifest rejected')
    manifest = json.loads(manifest_path.read_text())
    hashes = manifest.get('hashes')
    if not isinstance(hashes, dict) or not hashes:
        raise ValueError('Nonempty evidence inventory required')
    for name, expected in hashes.items():
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or path.as_posix() != name or name == 'manifest.json':
            raise ValueError('Unsafe evidence inventory path')
        target = root / path
        if any(p.is_symlink() for p in [target, *target.parents] if p != root.parent):
            raise ValueError('Symlink evidence rejected')
        if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            raise ValueError('Evidence missing or hash mismatch')
    actual = set()
    for path in root.rglob('*'):
        if path.is_symlink():
            raise ValueError('Symlink evidence rejected')
        if path.is_file() and path != manifest_path:
            actual.add(path.relative_to(root).as_posix())
    if actual != set(hashes):
        raise ValueError('Evidence inventory mismatch')
    return {'integrity_verified': True, 'files_verified': len(hashes),
            'acquisition_complete': manifest.get('complete') is True,
            'qualification_allowed': False,
            'limitation': 'Hashes are not signatures or proof of source accuracy or historical availability.'}


def _pages(recorder, endpoint, params, max_pages):
    token, seen = None, set()
    for _ in range(max_pages):
        request = dict(params)
        if token:
            request["page_token"] = token
        payload = recorder.request(endpoint, request)
        if not isinstance(payload, dict):
            raise ValueError("Expected paginated object response")
        yield payload
        token = payload.get("next_page_token")
        if token is None or token == "":
            return
        if not isinstance(token, str) or token in seen:
            raise ValueError("Repeated or invalid pagination token")
        seen.add(token)
    raise ValueError("Page limit reached; acquisition is incomplete")


def _frames(bars, first, last):
    result = {}
    for symbol, rows in bars.items():
        if not rows:
            result[symbol] = pd.DataFrame(columns=["open", "high", "low", "close", "volume"],
                                         index=pd.DatetimeIndex([], tz="UTC"))
            continue
        frame = pd.DataFrame(rows).rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
        required = ["t", "open", "high", "low", "close", "volume"]
        if any(column not in frame for column in required):
            raise ValueError("Incomplete bar schema")
        frame.index = pd.to_datetime(frame.pop("t"), utc=True)
        if frame.index.has_duplicates:
            raise ValueError("Duplicate bars across pages")
        if any(not first <= stamp.tz_convert("America/New_York").date().isoformat() <= last for stamp in frame.index):
            raise ValueError("Bar outside requested session dates")
        frame = frame[required[1:]].astype(float).sort_index()
        import numpy as np
        if not np.isfinite(frame.to_numpy()).all():
            raise ValueError("Nonfinite bar values")
        if (frame[["open", "high", "low", "close"]] <= 0).any().any() or (frame.volume < 0).any():
            raise ValueError("Invalid bar price or volume")
        if (frame.high < frame[["open", "close", "low"]].max(axis=1)).any() or (frame.low > frame[["open", "close", "high"]].min(axis=1)).any():
            raise ValueError("Invalid bar OHLC ordering")
        result[symbol] = frame
    return result


def acquire_dataset(kind, start, end, output, *, symbols=ETF_UNIVERSE, feed="sip",
                    adjustment="raw", fetcher=None, max_pages=10000):
    """Fetch one immutable dataset; failures preserve partial evidence and raise."""
    if kind not in KINDS or tuple(symbols) != ETF_UNIVERSE:
        raise ValueError("Use an allowed dataset kind and the exact fixed five-ETF universe")
    if feed not in {"sip", "iex"} or adjustment not in {"raw", "split", "dividend", "all"}:
        raise ValueError("Explicit supported feed/adjustment required")
    if max_pages < 1:
        raise ValueError("max_pages must be positive")
    first, last = _interval(start, end)
    metadata = {"provider": "alpaca", "kind": kind, "symbols": list(symbols),
                "start": first, "end": last, "feed": feed if kind in {"minute", "daily", "quotes"} else None,
                "adjustment": adjustment if kind in {"minute", "daily"} else None,
                "evaluation_role": "development_only", "missingness_policy": "causal_no_whole_day_deletion"}
    recorder = EvidenceRecorder(output, fetcher)
    try:
        calendar_payload = recorder.request("calendar", {"start": first, "end": last})
        calendar = parse_calendar(calendar_payload, first, last)
        (recorder.output / "calendar.json").write_text(json.dumps(calendar, indent=2))
        bars = {symbol: [] for symbol in symbols}
        record_count = 0
        if kind != "calendar":
            params = {"symbols": ",".join(symbols), "start": first, "end": last, "limit": 10000, "sort": "asc"}
            endpoint = {"minute": "bars", "daily": "bars", "quotes": "quotes", "actions": "actions"}[kind]
            if endpoint in {"bars", "quotes"}:
                params.update(start=pd.Timestamp(first, tz="America/New_York").tz_convert("UTC").isoformat(),
                              end=(pd.Timestamp(last, tz="America/New_York") + pd.DateOffset(days=1) - pd.Timedelta(microseconds=1)).tz_convert("UTC").isoformat(),
                              feed=feed)
            if endpoint == "bars":
                params.update(timeframe="1Min" if kind == "minute" else "1Day", adjustment=adjustment, asof=last)
            if endpoint == "actions":
                params["limit"] = 1000
            with (recorder.output / "records.jsonl").open("w") as records:
                for payload in _pages(recorder, endpoint, params, max_pages):
                    collection = payload.get({"bars": "bars", "quotes": "quotes", "actions": "corporate_actions"}[endpoint])
                    if not isinstance(collection, dict):
                        raise ValueError("Invalid response collection")
                    for group, values in collection.items():
                        if not isinstance(values, list):
                            raise ValueError("Invalid response records")
                        if endpoint != "actions" and group not in bars:
                            raise ValueError("Unexpected symbol")
                        for row in values:
                            if not isinstance(row, dict):
                                raise ValueError("Invalid record")
                            if endpoint == "actions":
                                record = {"action_type": group, "raw": row}
                            else:
                                record = dict(row, symbol=group)
                            records.write(json.dumps(record, sort_keys=True) + "\n")
                            record_count += 1
                        if endpoint == "bars":
                            bars[group].extend(values)
            if endpoint == "bars":
                frames = _frames(bars, first, last)
                for symbol, frame in frames.items():
                    frame.to_parquet(recorder.output / f"{symbol}.parquet")
                coverage = coverage_audit(frames, calendar, timeframe=params["timeframe"])
                (recorder.output / "coverage.json").write_text(json.dumps(coverage, indent=2))
                metadata["coverage_complete"] = bool(calendar) and all(row["complete"] for row in coverage)
                metadata["timeframe"] = params["timeframe"]
        metadata["record_count"] = record_count
        metadata["session_count"] = len(calendar)
        return recorder.finish(metadata, True)
    except Exception as exc:
        # Provider error text may contain unknown data. Store type, not arbitrary message.
        recorder.finish(dict(metadata, failure_type=type(exc).__name__), False)
        raise


def probe_entitlements(output, *, fetcher=None, session="2024-01-03"):
    """Small read-only probes; access success is not a purchased subscription."""
    first, last = _interval(session, session)
    recorder = EvidenceRecorder(output, fetcher)
    start = pd.Timestamp(first + " 10:00", tz="America/New_York").tz_convert("UTC")
    results = {}
    requests = {
        "calendar": ("calendar", {"start": first, "end": last}),
        "minute_sip": ("bars", {"symbols": ",".join(ETF_UNIVERSE), "timeframe": "1Min", "start": start.isoformat(), "end": (start + pd.Timedelta(minutes=1)).isoformat(), "feed": "sip", "adjustment": "raw", "limit": 5}),
        "daily_2016": ("bars", {"symbols": ",".join(ETF_UNIVERSE), "timeframe": "1Day", "start": "2016-01-04T05:00:00Z", "end": "2016-01-05T04:59:59Z", "feed": "sip", "adjustment": "raw", "limit": 5}),
        "quotes_sip": ("quotes", {"symbols": ",".join(ETF_UNIVERSE), "start": start.isoformat(), "end": (start + pd.Timedelta(seconds=1)).isoformat(), "feed": "sip", "limit": 5}),
        "actions": ("actions", {"symbols": ",".join(ETF_UNIVERSE), "start": "2024-01-01", "end": "2024-01-31", "limit": 5}),
    }
    for name, (kind, params) in requests.items():
        try:
            response = recorder.request(kind, params)
            collection = response if kind == "calendar" else response.get({"bars": "bars", "quotes": "quotes", "actions": "corporate_actions"}[kind])
            if not isinstance(collection, (dict, list)):
                raise ValueError("Invalid probe schema")
            count = sum(len(values) for values in collection.values() if isinstance(values, list)) if isinstance(collection, dict) else len(collection)
            results[name] = {"status": "accessible_with_records" if count else "accessible_empty", "sample_records": count,
                             "scope": "sample_request_only_not_complete_history"}
        except Exception as exc:
            results[name] = {"status": "unverified_or_unavailable", "failure_type": type(exc).__name__, "http_status": getattr(exc, "status_code", None)}
    (recorder.output / "probe.json").write_text(json.dumps(results, indent=2))
    recorder.finish({"kind": "entitlement_probe", "purchases": False, "results": results}, True)
    return results
