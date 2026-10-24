from __future__ import annotations

import random
from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from sim.mt940 import (
    REMITTANCE_MAX,
    MT940Error,
    Statement,
    StatementLine,
    parse_mt940,
    truncate_remittance,
    write_mt940,
)


def sample_statement() -> Statement:
    lines = [
        StatementLine(value_date=date(2026, 10, 2), entry_date=date(2026, 10, 2), dc="D",
                      amount_minor=18743012, txn_type="NTRF", customer_ref="PAY-BATCH-0001",
                      bank_ref="BREF000001", remittance="/REMI/FA-2025-0917 FA-2025-0918 FA-2025-0919 HORAK SEDACKY S.R.O."),  # fmt: skip
        StatementLine(value_date=date(2026, 10, 2), entry_date=date(2026, 10, 2), dc="C",
                      amount_minor=42000000, txn_type="NTRF", customer_ref="NONREF",
                      bank_ref="BREF000002", remittance="/ORDP/TOYOTA MOTOR EUROPE NV/REMI/SETTLEMENT 2026-09"),  # fmt: skip
        StatementLine(value_date=date(2026, 10, 2), entry_date=None, dc="D", amount_minor=4500,
                      txn_type="NCHG", customer_ref="NONREF", bank_ref=None,
                      remittance="ACCOUNT MAINTENANCE FEE 09/2026"),  # fmt: skip
    ]
    opening = 196200000
    closing = opening - 18743012 + 42000000 - 4500
    return Statement(statement_id="STMT261002EUR", account_iban="DE89370400440532013000", currency="EUR",
                     statement_date=date(2026, 10, 2), sequence_no=2, opening_balance_minor=opening,
                     lines=lines, closing_balance_minor=closing)  # fmt: skip


def test_writer_emits_required_tags_and_formats() -> None:
    text = write_mt940(sample_statement())
    assert text.startswith(":20:STMT261002EUR\r\n")
    assert ":25:DE89370400440532013000" in text
    assert ":28C:" in text
    assert ":60F:C261002EUR1962000,00" in text
    assert ":61:2610021002D187430,12NTRFPAY-BATCH-0001//BREF000001" in text
    assert ":61:261002D45,00NCHGNONREF" in text
    assert ":62F:C261002EUR2194525,88" in text
    assert text.count(":86:") == 3
    assert text.endswith("-\r\n")
    # :86: content is wrapped at 65 characters per SWIFT line rules
    for raw in text.split("\r\n"):
        assert len(raw) <= 69, raw


def test_round_trip_exact() -> None:
    stmt = sample_statement()
    text = write_mt940(stmt)
    parsed = parse_mt940(text)
    assert parsed == stmt
    assert write_mt940(parsed) == text


def test_remittance_truncated_to_140_chars() -> None:
    long = "X" * 300
    assert len(truncate_remittance(long)) == REMITTANCE_MAX == 140
    line = StatementLine(value_date=date(2026, 10, 3), entry_date=None, dc="C", amount_minor=100,
                         txn_type="NTRF", customer_ref="NONREF", bank_ref=None, remittance=long)  # fmt: skip
    assert len(line.remittance) == 140
    stmt = Statement(statement_id="S1", account_iban="DE89370400440532013000", currency="EUR",
                     statement_date=date(2026, 10, 3), sequence_no=3, opening_balance_minor=0,
                     lines=[line], closing_balance_minor=100)  # fmt: skip
    assert parse_mt940(write_mt940(stmt)).lines[0].remittance == "X" * 140


def test_statement_balance_consistency_is_enforced() -> None:
    with pytest.raises(ValidationError, match="closing"):
        Statement(statement_id="S1", account_iban="DE89370400440532013000", currency="EUR",
                  statement_date=date(2026, 10, 3), sequence_no=3, opening_balance_minor=0,
                  lines=[], closing_balance_minor=5)  # fmt: skip


def test_parser_rejects_garbage_and_inconsistent_text() -> None:
    with pytest.raises(MT940Error):
        parse_mt940("hello")
    text = write_mt940(sample_statement()).replace(":62F:C261002EUR2194525,88", ":62F:C261002EUR1,00")
    with pytest.raises(MT940Error):
        parse_mt940(text)


def test_negative_balances_and_zero_decimal_currency() -> None:
    stmt = Statement(statement_id="S9", account_iban="DE89370400440532013000", currency="JPY",
                     statement_date=date(2026, 10, 3), sequence_no=1, opening_balance_minor=-500,
                     lines=[StatementLine(value_date=date(2026, 10, 3), entry_date=None, dc="C",
                                          amount_minor=1200, txn_type="NTRF", customer_ref="NONREF",
                                          bank_ref=None, remittance="TEST")],
                     closing_balance_minor=700)  # fmt: skip
    text = write_mt940(stmt)
    assert ":60F:D261003JPY500," in text and ":62F:C261003JPY700," in text
    assert parse_mt940(text) == stmt


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_random_statements_round_trip(seed: int) -> None:
    rng = random.Random(seed)
    currency = rng.choice(["EUR", "CZK", "PLN"])
    day = date(2026, 10, 1) + timedelta(days=rng.randrange(46))
    opening = rng.randrange(-10_000_00, 500_000_000)
    lines = []
    balance = opening
    for i in range(rng.randrange(0, 8)):
        amt = rng.randrange(1, 300_000_00)
        dc = rng.choice(["D", "C"])
        balance += amt if dc == "C" else -amt
        rem = "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 /-.,") for _ in range(rng.randrange(0, 200)))
        lines.append(StatementLine(value_date=day, entry_date=rng.choice([None, day]), dc=dc, amount_minor=amt,
                                   txn_type=rng.choice(["NTRF", "NMSC", "NCHG", "NFEX"]),
                                   customer_ref=rng.choice(["NONREF", f"REF{i:04d}"]),
                                   bank_ref=rng.choice([None, f"B{i:05d}"]), remittance=rem.strip()))  # fmt: skip
    stmt = Statement(statement_id=f"R{seed}", account_iban="CZ6508000000192000145399", currency=currency,
                     statement_date=day, sequence_no=seed, opening_balance_minor=opening, lines=lines,
                     closing_balance_minor=balance)  # fmt: skip
    assert parse_mt940(write_mt940(stmt)) == stmt
