from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from sim.fx import (
    CURRENCIES,
    SOURCE_WINDOW_START,
    TARGET_START,
    WINDOW_DAYS,
    FxFetchError,
    build_fx_table,
    fetch_ecb_history,
    load_fx_rates,
    parse_ecb_csv,
    rate_for,
    write_fx_rates,
)

CSV = """Date,USD,JPY,BGN,CZK,DKK,GBP,HUF,PLN,RON,SEK,CHF,ISK,NOK,TRY,AUD,BRL,CAD,CNY,HKD,IDR,ILS,INR,KRW,MXN,MYR,NZD,PHP,SGD,THB,ZAR,
2026-08-05,1.0840,162.30,1.9558,24.3450,7.4600,0.8450,395.10,4.2680,4.97,11.40,0.9400,N/A,11.70,38.5,1.62,6.0,1.48,7.8,8.5,17500,4.0,91.0,1500,20.0,4.8,1.80,62.0,1.44,38.0,19.5,
"""


def synthetic_history(start: date, days: int) -> dict[date, dict[str, Decimal]]:
    """Business days only, like the ECB file; rates drift a little each day."""
    hist: dict[date, dict[str, Decimal]] = {}
    base = {
        "USD": Decimal("1.0840"),
        "JPY": Decimal("162.30"),
        "CZK": Decimal("24.3450"),
        "PLN": Decimal("4.2680"),
        "GBP": Decimal("0.8450"),
        "CHF": Decimal("0.9400"),
        "HUF": Decimal("395.10"),
    }
    for i in range(-7, days + 1):
        d = start + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        hist[d] = {k: v + Decimal(i) * Decimal("0.0001") for k, v in base.items()}
    return hist


def test_parse_ecb_csv_skips_na_and_keeps_decimals() -> None:
    hist = parse_ecb_csv(CSV)
    assert date(2026, 8, 5) in hist
    row = hist[date(2026, 8, 5)]
    assert row["CZK"] == Decimal("24.3450") and isinstance(row["CZK"], Decimal)
    assert "ISK" not in row


def test_build_fx_table_has_46_dates_eur_base_no_gaps() -> None:
    hist = synthetic_history(SOURCE_WINDOW_START, WINDOW_DAYS)
    table = build_fx_table(
        hist,
        source_start=SOURCE_WINDOW_START,
        target_start=TARGET_START,
        days=WINDOW_DAYS,
        currencies=CURRENCIES,
    )
    assert table.base == "EUR"
    assert len(table.rates) == 46
    dates = sorted(date.fromisoformat(d) for d in table.rates)
    assert dates[0] == date(2026, 10, 1) and dates[-1] == date(2026, 11, 15)
    assert dates == [date(2026, 10, 1) + timedelta(days=i) for i in range(46)]
    for d, day in table.rates.items():
        assert set(day.rates) == set(CURRENCIES)
        for cur, val in day.rates.items():
            dec = Decimal(val)
            assert dec.is_finite() and dec > 0, (d, cur, val)
            assert "nan" not in val.lower()
        assert day.carried_forward == (date.fromisoformat(d).weekday() >= 5)
    sat = table.rates["2026-10-03"]
    fri = table.rates["2026-10-02"]
    assert sat.rates == fri.rates and sat.source_date == fri.source_date
    assert table.source_window.start == SOURCE_WINDOW_START.isoformat()
    assert rate_for(table, "CZK", date(2026, 10, 1)) == Decimal(fri.rates["CZK"]) - Decimal(
        "0.0001"
    )


def test_build_fx_table_raises_instead_of_inventing_rates() -> None:
    hist = synthetic_history(SOURCE_WINDOW_START, WINDOW_DAYS)
    for d in list(hist):
        if d >= SOURCE_WINDOW_START + timedelta(days=20):
            del hist[d]  # a 26-day hole: carry-forward must refuse
    with pytest.raises(FxFetchError, match="no ECB rate"):
        build_fx_table(
            hist,
            source_start=SOURCE_WINDOW_START,
            target_start=TARGET_START,
            days=WINDOW_DAYS,
            currencies=CURRENCIES,
        )
    hist2 = synthetic_history(SOURCE_WINDOW_START, WINDOW_DAYS)
    for row in hist2.values():
        row.pop("PLN", None)
    with pytest.raises(FxFetchError, match="PLN"):
        build_fx_table(
            hist2,
            source_start=SOURCE_WINDOW_START,
            target_start=TARGET_START,
            days=WINDOW_DAYS,
            currencies=CURRENCIES,
        )


def test_write_and_load_round_trip(tmp_path) -> None:
    hist = synthetic_history(SOURCE_WINDOW_START, WINDOW_DAYS)
    table = build_fx_table(
        hist,
        source_start=SOURCE_WINDOW_START,
        target_start=TARGET_START,
        days=WINDOW_DAYS,
        currencies=CURRENCIES,
    )
    path = tmp_path / "fx_rates.json"
    write_fx_rates(table, path)
    assert load_fx_rates(path) == table
    assert write_fx_rates(table, tmp_path / "again.json") == (tmp_path / "again.json").read_bytes()


def test_fetch_failure_is_loud(tmp_path) -> None:
    with pytest.raises(FxFetchError, match="ECB"):
        fetch_ecb_history(
            cache_path=tmp_path / "none.zip", url="http://127.0.0.1:9/nope.zip", timeout=2
        )
    assert not (tmp_path / "none.zip").exists()


def test_real_ecb_fetch_covers_pinned_window(tmp_path) -> None:
    cache = tmp_path / "eurofxref-hist.zip"
    hist = fetch_ecb_history(cache_path=cache)
    assert cache.exists() and cache.stat().st_size > 100_000
    table = build_fx_table(
        hist,
        source_start=SOURCE_WINDOW_START,
        target_start=TARGET_START,
        days=WINDOW_DAYS,
        currencies=CURRENCIES,
    )
    assert len(table.rates) == 46
    assert all(Decimal(v).is_finite() for day in table.rates.values() for v in day.rates.values())
    # second call must be served from the cache even if the network is unreachable
    hist2 = fetch_ecb_history(cache_path=cache, url="http://127.0.0.1:9/nope.zip", timeout=2)
    assert hist2 == hist
