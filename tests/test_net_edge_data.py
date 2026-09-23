import json
from pathlib import Path

import pandas as pd
import pytest

from trader_engine.data.alpaca_intraday import DataResponse, request_data_raw
from trader_engine.data.evidence import (
    ETF_UNIVERSE, acquire_dataset, causal_bar_window, coverage_audit,
    parse_calendar, probe_entitlements,
)
from trader_engine.data.quote_validation import QuotePolicy, validate_quote_batch

DAY = "2024-01-03"
CALENDAR = [{"date": DAY, "open": "09:30", "close": "16:00"}]


def bar(stamp="2024-01-03T14:30:00Z"):
    return {"t": stamp, "o": 100, "h": 101, "l": 99, "c": 100, "v": 1000}


def quote(stamp="2024-01-03T14:30:00Z"):
    return {"t": stamp, "bp": 100, "ap": 100.01, "bs": 10, "as": 10, "bx": "V", "ax": "V"}


def test_paginated_raw_lineage_and_coverage_preserves_observed(tmp_path):
    calls = []
    def fetch(kind, params):
        calls.append((kind, params))
        if kind == "calendar":
            payload = CALENDAR
        elif "page_token" not in params:
            payload = {"bars": {"SPY": [bar()]}, "next_page_token": "two"}
        else:
            payload = {"bars": {"QQQ": [bar("2024-01-03T14:31:00Z")]}, "next_page_token": None}
        raw = json.dumps(payload, indent=1).encode()
        return DataResponse(payload, raw, "2026-09-23T23:00:00Z")
    output = tmp_path / "evidence"
    manifest = acquire_dataset("minute", DAY, DAY, output, fetcher=fetch)
    assert manifest["complete"] and not manifest["coverage_complete"]
    assert manifest["record_count"] == 2
    assert len(pd.read_parquet(output / "SPY.parquet")) == 1
    assert len(pd.read_parquet(output / "GLD.parquet")) == 0
    lineage = [json.loads(line) for line in (output / "lineage.jsonl").read_text().splitlines()]
    assert len(lineage) == 3
    assert all(row["representation"] == "exact_http_response_bytes" for row in lineage)
    assert calls[1][1]["feed"] == "sip" and calls[1][1]["adjustment"] == "raw"
    assert calls[2][1]["page_token"] == "two"
    coverage = json.loads((output / "coverage.json").read_text())
    assert len(coverage) == 5 and all(not row["whole_session_deleted"] for row in coverage)
    assert next(row for row in coverage if row["symbol"] == "SPY")["observed_count"] == 1
    with pytest.raises(FileExistsError):
        acquire_dataset("minute", DAY, DAY, output, fetcher=fetch)
    assert len(calls) == 3


def test_pagination_cycle_preserves_failed_evidence(tmp_path):
    def fetch(kind, params):
        return CALENDAR if kind == "calendar" else {"bars": {}, "next_page_token": "loop"}
    output = tmp_path / "cycle"
    with pytest.raises(ValueError, match="pagination"):
        acquire_dataset("minute", DAY, DAY, output, fetcher=fetch)
    assert not json.loads((output / "manifest.json").read_text())["complete"]
    assert len(list((output / "raw").glob("*.json"))) == 3


def test_page_limit_is_never_complete(tmp_path):
    def fetch(kind, params):
        return CALENDAR if kind == "calendar" else {"quotes": {}, "next_page_token": "more"}
    output = tmp_path / "limit"
    with pytest.raises(ValueError, match="Page limit"):
        acquire_dataset("quotes", DAY, DAY, output, fetcher=fetch, max_pages=1)
    assert not json.loads((output / "manifest.json").read_text())["complete"]


def test_duplicate_bars_fail_instead_of_silent_deduplication(tmp_path):
    def fetch(kind, params):
        return CALENDAR if kind == "calendar" else {"bars": {"SPY": [bar(), bar()]}}
    with pytest.raises(ValueError, match="Duplicate bars"):
        acquire_dataset("minute", DAY, DAY, tmp_path / "duplicates", fetcher=fetch)


def test_calendar_dst_early_close_and_duplicate():
    sessions = parse_calendar([
        {"date": "2024-03-08", "open": "09:30", "close": "16:00"},
        {"date": "2024-03-11", "open": "09:30", "close": "16:00"},
        {"date": "2024-11-29", "open": "09:30", "close": "13:00"},
    ])
    assert sessions[0]["open"].endswith("14:30:00+00:00")
    assert sessions[1]["open"].endswith("13:30:00+00:00")
    assert (pd.Timestamp(sessions[2]["close"]) - pd.Timestamp(sessions[2]["open"])).total_seconds() == 210 * 60
    with pytest.raises(ValueError, match="Duplicate"):
        parse_calendar(CALENDAR + CALENDAR)


def test_future_missingness_cannot_change_earlier_eligibility():
    index = pd.date_range("2024-01-03T14:30:00Z", periods=10, freq="min")
    frame = pd.DataFrame({name: [100.] * 10 for name in ("open", "high", "low", "close", "volume")}, index=index)
    before, eligible = causal_bar_window(frame, "2024-01-03T14:35:00Z", 5)
    after, after_eligible = causal_bar_window(frame.iloc[:6], "2024-01-03T14:35:00Z", 5)
    assert eligible and after_eligible
    pd.testing.assert_frame_equal(before, after)
    _, missing = causal_bar_window(frame.drop(index[3]), "2024-01-03T14:35:00Z", 5)
    assert not missing


def test_stale_source_and_cache_are_distinct_and_raw_retained():
    raw = quote()
    result = validate_quote_batch({"quotes": {"SPY": raw}}, ("SPY",),
                                  received_at="2024-01-03T14:30:01Z", decision_at="2024-01-03T14:30:05Z")["SPY"]
    assert not result.valid and set(result.reasons) == {"stale_cache", "stale_source"}
    raw["bp"] = 1
    assert result.envelope.raw_payload["bp"] == 100
    assert result.source_age_seconds == 5 and result.cache_age_seconds == 4


def test_batch_isolates_bad_and_missing_symbols():
    good = quote()
    crossed = dict(good, bp=102)
    results = validate_quote_batch({"quotes": {"SPY": good, "QQQ": crossed}}, ("SPY", "QQQ", "IWM"),
                                  received_at="2024-01-03T14:30:00.1Z", decision_at="2024-01-03T14:30:00.2Z")
    assert results["SPY"].valid
    assert "crossed_quote" in results["QQQ"].reasons
    assert "missing_quote" in results["IWM"].reasons


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, None])
def test_nonfinite_or_invalid_prices_are_rejected(bad):
    result = validate_quote_batch({"quotes": {"SPY": dict(quote(), bp=bad)}}, ("SPY",),
                                  received_at="2024-01-03T14:30:00Z", decision_at="2024-01-03T14:30:00Z")["SPY"]
    assert not result.valid and "invalid_price" in result.reasons


def test_clock_feed_and_malformed_batch_are_diagnostic():
    result = validate_quote_batch({"quotes": {"SPY": quote("2024-01-03T14:30:02Z")}}, ("SPY",),
                                  received_at="2024-01-03T14:30:00Z", decision_at="2024-01-03T14:30:00Z", feed="iex")["SPY"]
    assert {"unexpected_feed", "future_event", "event_after_receipt"} <= set(result.reasons)
    results = validate_quote_batch(None, ("SPY",), received_at="bad", decision_at="bad")
    assert "invalid_batch_payload" in results["SPY"].reasons


def test_actions_archive_without_inventing_event_availability(tmp_path):
    def fetch(kind, params):
        if kind == "calendar": return CALENDAR
        return {"corporate_actions": {"cash_dividends": [{"symbol": "SPY", "ex_date": DAY, "rate": 1.0}]}}
    manifest = acquire_dataset("actions", DAY, DAY, tmp_path / "actions", fetcher=fetch)
    assert manifest["record_count"] == 1 and not manifest["publication_time_known"]
    record = json.loads((tmp_path / "actions" / "records.jsonl").read_text())
    assert record["action_type"] == "cash_dividends"
    assert "available_at" not in record


def test_probe_reports_empty_access_separately_from_failure(tmp_path):
    def fetch(kind, params):
        if kind == "calendar": return CALENDAR
        if kind == "quotes": raise RuntimeError("denied")
        if kind == "actions": return {"corporate_actions": {}}
        return {"bars": {"SPY": [bar()]}}
    result = probe_entitlements(tmp_path / "probe", fetcher=fetch)
    assert result["quotes_sip"]["status"] == "unverified_or_unavailable"
    assert result["actions"]["status"] == "accessible_empty"
    assert result["minute_sip"]["status"] == "accessible_with_records"


def test_no_endpoint_or_universe_escape(tmp_path):
    with pytest.raises(ValueError, match="Read-only"):
        request_data_raw("orders", {})
    with pytest.raises(ValueError, match="five-ETF"):
        acquire_dataset("minute", DAY, DAY, tmp_path / "bad", symbols=("TSLA",))
    assert not (tmp_path / "bad").exists()

