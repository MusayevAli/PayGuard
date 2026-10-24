"""ECB daily reference rates for the simulation window (spec §2.7, §4).

The full history file ``eurofxref-hist.zip`` is fetched from the ECB (and cached), a pinned
46-day source window is cut out of it and its dates are shifted onto 2026-10-01..2026-11-15.
Non-business days carry the last published rate forward and are flagged. A failed fetch or a
missing rate raises ``FxFetchError``; rates are never invented.
"""

from __future__ import annotations

import csv
import io
import ssl
import urllib.error
import urllib.request
import zipfile
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

ECB_HIST_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"
SOURCE_WINDOW_START = date(2026, 8, 6)  # a Thursday, so weekends line up with the target window
TARGET_START = date(2026, 10, 1)
WINDOW_DAYS = 46
CURRENCIES: tuple[str, ...] = ("CZK", "PLN", "USD", "JPY", "GBP", "CHF", "HUF")
LOOKBACK_DAYS = 7
DEFAULT_CACHE = Path(".cache") / "eurofxref-hist.zip"

History = dict[date, dict[str, Decimal]]


class FxFetchError(RuntimeError):
    """The ECB series could not be fetched or does not cover what the simulation needs."""


class SourceWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    start: str
    end: str


class DayRates(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    rates: dict[str, str] = Field(description="currency -> units per 1 EUR, as a decimal string")
    carried_forward: bool
    source_date: str


class FxTable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    base: str
    source: str
    source_window: SourceWindow
    rates: dict[str, DayRates]


def parse_ecb_csv(text: str) -> History:
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header or header[0].strip() != "Date":
        raise FxFetchError("ECB CSV does not start with a Date column")
    currencies = [h.strip() for h in header[1:]]
    history: History = {}
    for row in reader:
        if not row or not row[0].strip():
            continue
        try:
            day = date.fromisoformat(row[0].strip())
        except ValueError as exc:
            raise FxFetchError(f"bad date in ECB CSV: {row[0]!r}") from exc
        rates: dict[str, Decimal] = {}
        for cur, value in zip(currencies, row[1:], strict=False):
            v = value.strip()
            if not cur or not v or v.upper() == "N/A":
                continue
            try:
                dec = Decimal(v)
            except InvalidOperation:
                continue
            if dec.is_finite() and dec > 0:
                rates[cur] = dec
        history[day] = rates
    if not history:
        raise FxFetchError("ECB CSV contained no rate rows")
    return history


def _ssl_context() -> ssl.SSLContext:
    """Verify TLS against certifi's CA bundle; python-build-standalone ships without one."""
    import certifi

    return ssl.create_default_context(cafile=certifi.where())


def _read_zip(blob: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not names:
                raise FxFetchError("ECB zip contains no CSV file")
            return zf.read(names[0]).decode("utf-8-sig")
    except zipfile.BadZipFile as exc:
        raise FxFetchError("ECB download is not a zip file") from exc


def fetch_ecb_history(
    cache_path: Path | None = DEFAULT_CACHE, url: str = ECB_HIST_URL, timeout: float = 30.0
) -> History:
    """Return the full ECB daily history. Uses the cached zip when present; downloads otherwise."""
    if cache_path is not None and cache_path.exists():
        return parse_ecb_csv(_read_zip(cache_path.read_bytes()))
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=_ssl_context()) as resp:
            blob = resp.read()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise FxFetchError(
            f"could not fetch ECB reference rates from {url}: {exc}. "
            "Check network access or place a previously downloaded eurofxref-hist.zip at "
            f"{cache_path or DEFAULT_CACHE}. Rates are never invented."
        ) from exc
    text = _read_zip(blob)
    history = parse_ecb_csv(text)
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_bytes(blob)
        tmp.replace(cache_path)
    return history


def build_fx_table(
    history: History,
    *,
    source_start: date = SOURCE_WINDOW_START,
    target_start: date = TARGET_START,
    days: int = WINDOW_DAYS,
    currencies: tuple[str, ...] = CURRENCIES,
) -> FxTable:
    rates: dict[str, DayRates] = {}
    for i in range(days):
        source_day = source_start + timedelta(days=i)
        target_day = target_start + timedelta(days=i)
        carried = False
        lookup = source_day
        while lookup not in history:
            lookup -= timedelta(days=1)
            carried = True
            if (source_day - lookup).days > LOOKBACK_DAYS:
                raise FxFetchError(
                    f"no ECB rate within {LOOKBACK_DAYS} days before {source_day}; "
                    "the source window is not covered by the fetched history"
                )
        row = history[lookup]
        missing = [c for c in currencies if c not in row]
        if missing:
            raise FxFetchError(f"ECB row for {lookup} lacks {', '.join(missing)}")
        rates[target_day.isoformat()] = DayRates(
            rates={c: str(row[c]) for c in currencies},
            carried_forward=carried,
            source_date=lookup.isoformat(),
        )
    return FxTable(
        base="EUR",
        source="ECB euro foreign exchange reference rates (eurofxref-hist), dates shifted",
        source_window=SourceWindow(
            start=source_start.isoformat(),
            end=(source_start + timedelta(days=days - 1)).isoformat(),
        ),
        rates=rates,
    )


def write_fx_rates(table: FxTable, path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = table.model_dump_json(indent=2).encode("utf-8") + b"\n"
    path.write_bytes(data)
    return data


def load_fx_rates(path: Path) -> FxTable:
    return FxTable.model_validate_json(path.read_bytes())


def rate_for(table: FxTable, currency: str, on: date) -> Decimal:
    try:
        return Decimal(table.rates[on.isoformat()].rates[currency])
    except KeyError as exc:
        raise FxFetchError(f"no {currency} rate for {on}") from exc
