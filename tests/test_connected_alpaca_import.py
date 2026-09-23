import gzip
import importlib.util
import json
from pathlib import Path
import pandas as pd
import pytest

spec = importlib.util.spec_from_file_location("connected_import", Path(__file__).parents[1] / "scripts/import_connected_alpaca.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

def archive(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt") as handle:
        json.dump({"response": {"structuredContent": {"data": data}}}, handle)

def test_connected_import_preserves_prices_gaps_and_unpaid_distributions(tmp_path):
    source = tmp_path / "raw"
    calendar = [{"date": "2023-12-29", "open": "09:30", "close": "09:32"},
                {"date": "2024-01-02", "open": "09:30", "close": "09:32"}]
    archive(source / "calendar.json.gz", {"result": calendar})
    for symbol in module.ETF_UNIVERSE:
        bar = {"t": "2024-01-02T14:30:00Z", "o": 100, "h": 101, "l": 99, "c": 100, "v": 10}
        archive(source / symbol / "minute_000.json.gz", {"bars": {symbol: [bar]}})
        archive(source / symbol / "daily_raw.json.gz", {"bars": {symbol: [bar | {"t": "2023-12-29T05:00:00Z"}]}})
        archive(source / symbol / "daily_all.json.gz", {"bars": {symbol: [bar | {"t": "2023-12-29T05:00:00Z", "c": 50}]}})
    archive(source / "actions.json.gz", {"corporate_actions": {"cash_dividends": [
        {"id": "distribution-1", "symbol": "SPY", "ex_date": "2024-01-02", "rate": 1.0}
    ]}, "next_page_token": None})
    out = tmp_path / "normalized"
    manifest = module.normalize(source, out)
    daily = pd.read_parquet(out / "daily/SPY.parquet")
    assert daily.index.tz is not None
    assert daily.index[0].date().isoformat() == "2023-12-29"
    assert daily.signal_scale.iloc[0] == 2.0
    assert not manifest["coverage_complete"]
    assert manifest["counts"]["SPY"]["missing_minutes"] == 1
    actions = pd.read_parquet(out / "actions.parquet")
    assert actions.cash_dividend.iloc[0] == 1.0
    assert pd.isna(actions.payment_timestamp.iloc[0])
    schedule = pd.read_parquet(out / "schedule.parquet")
    assert schedule.previous_session.iloc[0] == "2023-12-29"
    assert len(pd.read_parquet(out / "minutes/SPY.parquet")) == 1
    with pytest.raises(FileExistsError):
        module.normalize(source, out)

def test_nested_provider_error_is_not_data(tmp_path):
    path = tmp_path / "bad.json.gz"
    archive(path, {"error": {"status": 403}})
    with pytest.raises(ValueError, match="Unsuccessful"):
        module.read(path)
