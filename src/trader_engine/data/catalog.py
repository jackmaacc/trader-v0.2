"""Instrument discovery only. A listing is not proof of data or trading access."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field, model_validator

Kind = Literal['equity', 'etf', 'crypto', 'forex', 'future', 'perpetual', 'option', 'bond', 'fund', 'warrant', 'unit', 'other']


class Instrument(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False, str_strip_whitespace=True)
    provider: str = Field(min_length=1)
    venue: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    name: str = Field(min_length=1)
    kind: Kind
    currency: str = ''
    base_currency: str = ''
    country: str = ''
    status: str = 'unknown'
    expiry: str | None = None
    strike: float | None = Field(default=None, ge=0)
    option_right: Literal['call', 'put'] | None = None
    contract_size: float | None = Field(default=None, gt=0)
    settlement_currency: str = ''
    underlying: str = ''
    source_url: str = ''
    observed_at: datetime
    history_status: Literal['unverified'] = 'unverified'
    execution_status: Literal['not_connected'] = 'not_connected'

    @model_validator(mode='after')
    def require_contract_identity(self):
        if self.observed_at.tzinfo is None:
            raise ValueError('observed_at must include a timezone')
        if self.kind in {'future', 'option'}:
            if not self.expiry or self.contract_size is None:
                raise ValueError('Dated derivatives require expiry and contract_size')
            datetime.fromisoformat(self.expiry.replace('Z', '+00:00'))
        if self.kind == 'option' and (self.strike is None or self.option_right is None):
            raise ValueError('Options require strike and option_right')
        if self.kind == 'perpetual' and self.contract_size is None:
            raise ValueError('Perpetuals require contract_size')
        return self

    @property
    def instrument_id(self):
        # Do not merge venue-specific pairs or distinct contracts by ticker alone.
        parts = [self.provider, self.venue, self.symbol, self.kind, self.expiry,
                 self.strike, self.option_right, self.contract_size, self.currency,
                 self.settlement_currency, self.underlying]
        return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def fetch_text(url: str) -> str:
    request = Request(url, headers={'User-Agent': 'TRADER2.0 market directory research/1.0'})
    with urlopen(request, timeout=30) as response:
        data = response.read(20_000_001)
    if len(data) > 20_000_000:
        raise ValueError('Directory response exceeds 20 MB limit')
    return data.decode('utf-8-sig')


def parse_nasdaq(text: str, source_url: str, observed_at: datetime, other: bool = False) -> list[Instrument]:
    lines = [line for line in text.splitlines() if line and not line.startswith('File Creation Time:')]
    reader = csv.DictReader(lines, delimiter='|')
    symbol_col = 'ACT Symbol' if other else 'Symbol'
    required = {symbol_col, 'Security Name', 'Test Issue', 'ETF'} | ({'Exchange'} if other else set())
    if not required.issubset(reader.fieldnames or []):
        raise ValueError('Unexpected Nasdaq directory schema')
    venues = {'N': 'NYSE', 'A': 'NYSE American', 'P': 'NYSE Arca', 'Z': 'Cboe BZX', 'V': 'IEX', 'M': 'NYSE Texas'}
    result = []
    for row in reader:
        if row['Test Issue'] == 'Y':
            continue
        name = row['Security Name']; lower = name.lower()
        kind = 'etf' if row['ETF'] == 'Y' else 'equity'
        if kind != 'etf':
            if 'warrant' in lower: kind = 'warrant'
            elif ' unit' in lower: kind = 'unit'
            elif any(word in lower for word in ('preferred', 'depositary share', 'notes due', ' rights')): kind = 'other'
        result.append(Instrument(provider='nasdaq_directory', venue=venues.get(row.get('Exchange'), row.get('Exchange', 'NASDAQ')),
            symbol=row[symbol_col], name=name, kind=kind, currency='USD', country='US',
            status='listed; trading status unverified', source_url=source_url, observed_at=observed_at))
    if not result:
        raise ValueError('Empty Nasdaq directory')
    return result


def parse_coinbase(payload, source_url, observed_at):
    if not isinstance(payload, list) or not payload:
        raise ValueError('Unexpected or empty Coinbase products response')
    return [Instrument(provider='coinbase', venue='Coinbase Exchange', symbol=r['id'], name=r.get('display_name') or r['id'],
        kind='crypto', base_currency=r['base_currency'], currency=r['quote_currency'],
        status='disabled' if r.get('trading_disabled') else r.get('status', 'unknown'),
        source_url=source_url, observed_at=observed_at) for r in payload]


def parse_kraken(payload, source_url, observed_at):
    if payload.get('error') or not payload.get('result'):
        raise ValueError(f"Kraken directory error: {payload.get('error')}")
    fiat = {'USD', 'EUR', 'GBP', 'JPY', 'CHF', 'CAD', 'AUD', 'NZD', 'AED', 'BRL', 'ARS', 'MXN'}
    result = []
    for symbol, r in payload['result'].items():
        display = r.get('wsname', '')
        base, _, quote = display.partition('/')
        # Unknown/non-currency products are retained as other, not silently called crypto.
        asset_class = r.get('aclass_base', 'currency')
        kind = 'forex' if base in fiat and quote in fiat else ('crypto' if asset_class == 'currency' and base and quote else 'other')
        result.append(Instrument(provider='kraken', venue='Kraken', symbol=symbol, name=display or symbol,
            kind=kind, base_currency=base, currency=quote, status=r.get('status', 'unknown'),
            source_url=source_url, observed_at=observed_at))
    return result


def parse_deribit(payload, source_url, observed_at):
    if payload.get('error') or not isinstance(payload.get('result'), list) or not payload['result']:
        raise ValueError('Unexpected or empty Deribit instruments response')
    result = []
    for r in payload['result']:
        kind = {'spot': 'crypto', 'future': 'future', 'option': 'option'}.get(r['kind'], 'other')
        if kind == 'future' and r.get('settlement_period') == 'perpetual': kind = 'perpetual'
        expiry = None
        if kind in {'future', 'option'}:
            expiry = datetime.fromtimestamp(r['expiration_timestamp']/1000, timezone.utc).isoformat()
        result.append(Instrument(provider='deribit', venue='Deribit', symbol=r['instrument_name'], name=r['instrument_name'],
            kind=kind, currency=r.get('quote_currency', ''), base_currency=r.get('base_currency', ''),
            status=r.get('state') or ('active; order status unverified' if r.get('is_active') else 'inactive'),
            expiry=expiry, strike=r.get('strike') if kind == 'option' else None,
            option_right=r.get('option_type') if kind == 'option' else None, contract_size=r.get('contract_size'),
            settlement_currency=r.get('settlement_currency', ''), underlying=r.get('price_index', ''),
            source_url=source_url, observed_at=observed_at))
    return result


SOURCES = {
    'nasdaq': ('https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt', 'Nasdaq-listed U.S. securities', lambda t,u,d: parse_nasdaq(t,u,d)),
    'other_us': ('https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt', 'Other U.S. exchange-listed securities', lambda t,u,d: parse_nasdaq(t,u,d,True)),
    'coinbase': ('https://api.exchange.coinbase.com/products', 'Coinbase Exchange pairs only', lambda t,u,d: parse_coinbase(json.loads(t),u,d)),
    'kraken': ('https://api.kraken.com/0/public/AssetPairs', 'Kraken pairs, including available fiat pairs only', lambda t,u,d: parse_kraken(json.loads(t),u,d)),
    'deribit': ('https://www.deribit.com/api/v2/public/get_instruments?currency=any&expired=false', 'Deribit instruments only; not global equity options or commodity futures', lambda t,u,d: parse_deribit(json.loads(t),u,d)),
}


def import_instruments(path: Path) -> list[Instrument]:
    """Explicit broker/vendor exports can add markets without pretending coverage exists."""
    if path.suffix.lower() == '.csv':
        with path.open(newline='') as handle:
            rows = [{k:v for k,v in row.items() if v != ''} for row in csv.DictReader(handle)]
    else:
        rows = json.loads(path.read_text())
    if not isinstance(rows, list) or not rows:
        raise ValueError('Import must be a nonempty list of instruments')
    return [Instrument.model_validate(row) for row in rows]


def discover(sources: list[str], imports: list[Path] = (), fetcher=fetch_text, *, alpaca=False, options_through=None):
    unknown = set(sources)-SOURCES.keys()
    if unknown: raise ValueError(f'Unknown directory sources: {sorted(unknown)}')
    now = datetime.now(timezone.utc)
    instruments = {}; coverage = []
    for source in dict.fromkeys(sources):
        url, scope, parser = SOURCES[source]
        try:
            text = fetcher(url)
            rows = parser(text,url,now)
            for item in rows: instruments[item.instrument_id] = item
            coverage.append(dict(source=source, scope=scope, status='ok', instrument_count=len(rows),
                source_url=url, sha256=hashlib.sha256(text.encode()).hexdigest()))
        except Exception as exc:
            coverage.append(dict(source=source, scope=scope, status='failed', instrument_count=0, error=str(exc)))
    if alpaca:
        from trader_engine.data.alpaca_catalog import discover_alpaca
        try:
            rows, alpaca_coverage = discover_alpaca(options_through)
            for item in rows: instruments[item.instrument_id] = item
            coverage.extend(alpaca_coverage)
        except Exception as exc:
            coverage.append(dict(source='alpaca', scope='Alpaca paper directory', status='failed', instrument_count=0, error=str(exc)))
    # Invalid imports are errors, not partially accepted files.
    for path in imports:
        rows = import_instruments(path)
        for item in rows: instruments[item.instrument_id] = item
        coverage.append(dict(source=str(path), scope='User-supplied directory; coverage unverified', status='ok', instrument_count=len(rows)))
    return dict(schema_version=1, observed_at=now.isoformat(), worldwide_complete=False,
        coverage=coverage, limitations=[
            'Current directories are not historical point-in-time universes and omit delisted history.',
            'No connected broker: listing does not establish account, jurisdiction, or order access.',
            'Price history and quotes have not been verified; catalog entries are not backtest-ready.',
            'Global equities, OTC, bonds, U.S. listed options, and non-Deribit futures need additional directories and feeds.',
            'Forex and crypto coverage is restricted to the named venues; no worldwide consolidated catalog is claimed.'
        ], instruments=[dict(instrument_id=i.instrument_id, **i.model_dump(mode='json')) for i in sorted(instruments.values(), key=lambda i:(i.kind,i.venue,i.symbol))])


def save_catalog(payload: dict, path: Path):
    if not payload['instruments']:
        raise ValueError('No instruments discovered; existing catalog retained')
    path.parent.mkdir(parents=True, exist_ok=True)
    # Publish one self-contained snapshot; readers never see a half-written JSON file.
    fd, temporary = tempfile.mkstemp(prefix='.catalog-', suffix='.json', dir=path.parent)
    try:
        with os.fdopen(fd,'w') as handle:
            json.dump(payload,handle,indent=2,allow_nan=False)
            handle.flush();os.fsync(handle.fileno())
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
