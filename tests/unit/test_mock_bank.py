"""Mock bank (spec §11): HMAC mismatch rejected, settlement moves balance and appears in the next
statement, MT940 round-trip, idempotent line parsing, fees never emitted twice."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from bank.mock_bank import MockBank
from core.bootstrap import OPENING_BANK_MINOR
from core.events import EventStore, make_event
from core.money import Money, convert
from core.vendors import account_hash
from signer.frames import PayFrame, encode, hmac_tag
from sim.models import Event
from sim.mt940 import parse_mt940, write_mt940
from sim.worldgen.catalog import BANK_ACCOUNTS
from tests.agentkit import full_synthetic_fx

KEY = bytes.fromhex("bb" * 32)
NOW = datetime(2026, 10, 6, tzinfo=UTC)
IBAN = "CZ6701000000009198260449"


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.store = EventStore(tmp_path / "pg.db")
        self.scheduled: list[Event] = []
        self.now = NOW
        self.bank = MockBank(
            tmp_path / "pg.db",
            store=self.store,
            key=KEY,
            fx=full_synthetic_fx(),
            now=lambda: self.now,
            schedule=self.scheduled.append,
            out_dir=tmp_path,
        )

    def request(
        self, pay_id: str, amount: int, currency: str = "EUR", eur: int | None = None
    ) -> Event:
        rate_date = date(2026, 10, 6)
        if currency == "EUR":
            eur_minor, rate_text, rate_iso = (eur if eur is not None else amount), None, None
        else:
            conv = convert(Money(amount, currency), "EUR", rate_date, full_synthetic_fx())
            eur_minor = eur if eur is not None else conv.money.minor
            rate_text, rate_iso = str(conv.rate), rate_date.isoformat()
        payload = {
            "pay_id": pay_id,
            "payable_ids": ["AP-000001"],
            "vendor_id": "V-0001",
            "account_hash": account_hash(IBAN),
            "account_masked": "CZ67 **** 0449",
            "amount_minor": amount,
            "currency": currency,
            "eur_equiv_minor": eur_minor,
            "fx_rate": rate_text,
            "rate_date": rate_iso,
            "requested_by": "payment_agent",
            "frame_hex": None,
        }
        return self.store.append(
            make_event(
                "payment_requested",
                payload,
                event_time=self.now,
                source="agent",
                actor="payment_agent",
            )
        ).event

    def frame(self, seq: int, pay_id: str, eur: int, currency: str = "EUR") -> bytes:
        return encode(
            PayFrame(
                seq=seq,
                account_hash=bytes.fromhex(account_hash(IBAN)),
                amount_minor=eur,
                currency=currency,
                pay_id=pay_id,
            )
        )


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def test_opening_balances_come_from_bootstrap(env: Env) -> None:
    assert env.bank.balances() == OPENING_BANK_MINOR


def test_bad_tag_is_rejected_with_an_event_and_no_balance_change(env: Env) -> None:
    env.request("PAY-000001", 1_112_000)
    frame = env.frame(1, "PAY-000001", 1_112_000)
    res = env.bank.execute(frame, hmac_tag(bytes(32), frame))
    assert not res.ok and res.reason == "bad_tag"
    rejected = env.store.query(type="bank_rejected")
    assert (
        len(rejected) == 1
        and rejected[0].payload["pay_id"] == "PAY-000001"
        and rejected[0].payload["reason"] == "bad_tag"
    )
    assert env.bank.balance("EUR") == OPENING_BANK_MINOR["EUR"]


def test_unknown_payment_and_frame_mismatch_are_rejected(env: Env) -> None:
    frame = env.frame(1, "PAY-000009", 5)
    assert env.bank.execute(frame, hmac_tag(KEY, frame)).reason == "unknown_payment"
    env.request("PAY-000002", 1000)
    frame = env.frame(1, "PAY-000002", 999)
    assert env.bank.execute(frame, hmac_tag(KEY, frame)).reason == "frame_mismatch"


def test_settlement_moves_balance_and_lands_in_the_next_statement(env: Env) -> None:
    env.request("PAY-000001", 1_112_000)
    frame = env.frame(1, "PAY-000001", 1_112_000)
    res = env.bank.execute(frame, hmac_tag(KEY, frame))
    assert res.ok and res.bank_ref == "E261006P001" and res.value_date == date(2026, 10, 6)
    assert env.bank.balance("EUR") == OPENING_BANK_MINOR["EUR"] - 1_112_000
    events = env.bank.publish_statements(through=date(2026, 10, 6))
    assert [e.type for e in events] == ["bank_statement", "bank_statement_line"]
    stmt_ev, line_ev = events
    assert stmt_ev.known_at == datetime(
        2026, 10, 7, 6, 0, tzinfo=UTC
    ) and stmt_ev.event_time == datetime(2026, 10, 7, 5, 30, tzinfo=UTC)
    parsed = parse_mt940(stmt_ev.payload["mt940"])
    assert parsed.statement_id == "EUR261006P" and parsed.closing_balance_minor == -1_112_000
    assert (
        parsed.lines[0].customer_ref == "PAY-000001"
        and parsed.lines[0].bank_ref == "E261006P001"
        and parsed.lines[0].dc == "D"
    )
    assert len(parsed.lines[0].remittance) <= 140
    assert (
        line_ev.payload["customer_ref"] == "PAY-000001"
        and line_ev.payload["amount_minor"] == 1_112_000
    )
    assert env.scheduled == events, "statements are delivered through the clock, not appended now"
    assert env.bank.publish_statements(through=date(2026, 10, 6)) == [], "published once"
    assert (Path(env.bank._out_dir) / stmt_ev.payload["path"]).read_bytes().decode(
        "utf-8"
    ) == stmt_ev.payload["mt940"]


def test_usd_payment_settles_from_the_eur_account_at_the_days_rate(env: Env) -> None:
    env.request("PAY-000003", 10_800, "USD", eur=10_000)
    frame = env.frame(1, "PAY-000003", 10_000, "USD")
    res = env.bank.execute(frame, hmac_tag(KEY, frame))
    assert (
        res.ok
        and res.settlement_currency == "EUR"
        and res.settlement_amount_minor == 10_000
        and res.fx_rate is not None
    )
    assert env.bank.balance("EUR") == OPENING_BANK_MINOR["EUR"] - 10_000


def test_insufficient_funds_is_rejected(env: Env) -> None:
    ev = env.request("PAY-000004", OPENING_BANK_MINOR["PLN"] + 100, "PLN")
    frame = env.frame(1, "PAY-000004", ev.payload["eur_equiv_minor"], "PLN")
    assert env.bank.execute(frame, hmac_tag(KEY, frame)).reason == "insufficient_funds"
    assert env.bank.balance("PLN") == OPENING_BANK_MINOR["PLN"]


def _world_statement(day: date, lines: list[tuple[str, int, str, str, str]], opening: int) -> Event:
    from sim.mt940 import Statement, StatementLine

    stmt_lines = [
        StatementLine(
            value_date=day,
            entry_date=day,
            dc=dc,
            amount_minor=amt,
            txn_type=typ,
            customer_ref=ref,
            bank_ref=None,
            remittance=rem,
        )
        for dc, amt, typ, ref, rem in lines
    ]  # type: ignore[arg-type]
    closing = opening + sum(a if d == "C" else -a for d, a, *_ in lines)
    stmt = Statement(
        statement_id=f"EUR{day:%y%m%d}",
        account_iban=BANK_ACCOUNTS["EUR"]["iban"],
        currency="EUR",
        statement_date=day,
        sequence_no=1,
        opening_balance_minor=opening,
        lines=stmt_lines,
        closing_balance_minor=closing,
    )
    text = write_mt940(stmt)
    nxt = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(days=1)
    return make_event(
        "bank_statement",
        {
            "statement_id": stmt.statement_id,
            "account_iban": stmt.account_iban,
            "currency": "EUR",
            "statement_date": day.isoformat(),
            "sequence_no": 1,
            "path": "x",
            "mt940": text,
        },
        event_time=nxt.replace(hour=5, minute=30),
        known_at=nxt.replace(hour=6),
        source="bank",
        actor="INGDDEFFXXX",
    )


def test_ingest_parses_lines_once_and_tracks_the_world_balance(env: Env) -> None:
    ev = _world_statement(
        date(2026, 10, 1),
        [
            ("D", 4500, "NCHG", "NONREF", "ACCOUNT MAINTENANCE FEE 09/2026"),
            ("C", 100, "NMSC", "REF1", "INTEREST"),
        ],
        OPENING_BANK_MINOR["EUR"],
    )
    env.store.append(ev)
    first = env.bank.ingest(ev)
    assert len(first) == 2 and {e.payload["txn_type"] for e in first} == {"NCHG", "NMSC"}
    assert env.bank.ingest(ev) == [], "second ingest emits nothing"
    assert len(env.store.query(type="bank_statement_line")) == 2, "no double fees"
    assert env.bank.balance("EUR") == OPENING_BANK_MINOR["EUR"] - 4500 + 100
    # the world's own line events (same payload) also count as seen
    ev2 = _world_statement(
        date(2026, 10, 2), [("D", 50, "NCHG", "NONREF", "FEE")], env.bank.balance("EUR")
    )
    env.store.append(ev2)
    env.store.append(
        make_event(
            "bank_statement_line",
            {
                **json.loads(
                    json.dumps(
                        {
                            "statement_id": ev2.payload["statement_id"],
                            "account_iban": ev2.payload["account_iban"],
                            "currency": "EUR",
                            "line_no": 1,
                            "value_date": "2026-10-02",
                            "entry_date": "2026-10-02",
                            "dc": "D",
                            "amount_minor": 50,
                            "txn_type": "NCHG",
                            "customer_ref": "NONREF",
                            "bank_ref": None,
                            "remittance": "FEE",
                        }
                    )
                )
            },
            event_time=datetime(2026, 10, 2, 12, tzinfo=UTC),
            source="bank",
            actor="x",
        )
    )
    assert env.bank.ingest(ev2) == []
    assert len(env.store.query(type="bank_statement_line")) == 3


def test_statement_gap_is_reported_not_swallowed(env: Env) -> None:
    ev = _world_statement(
        date(2026, 10, 1), [("D", 1, "NCHG", "NONREF", "FEE")], OPENING_BANK_MINOR["EUR"]
    )
    env.store.append(ev)
    env.bank.ingest(ev)
    gap = _world_statement(
        date(2026, 10, 2), [("D", 1, "NCHG", "NONREF", "FEE")], OPENING_BANK_MINOR["EUR"] + 999
    )
    env.store.append(gap)
    env.bank.ingest(gap)
    errors = env.store.query(type="system_error")
    assert any(e.payload["code"] == "statement_gap" for e in errors)


def test_mt940_round_trip_of_the_banks_statement(env: Env) -> None:
    env.request("PAY-000001", 777_00)
    frame = env.frame(1, "PAY-000001", 777_00)
    env.bank.execute(frame, hmac_tag(KEY, frame))
    env.now = datetime(2026, 10, 6, 12, tzinfo=UTC)
    ref = env.bank.credit(
        "EUR",
        5_000_00,
        reference="ICO-DRAW-0001",
        remittance="/REMI/INTERCOMPANY DRAW TBJ TREASURY",
    )
    assert ref == "E261006P002"
    stmt_ev = env.bank.publish_statements(through=date(2026, 10, 6))[0]
    parsed = parse_mt940(stmt_ev.payload["mt940"])
    assert write_mt940(parsed) == stmt_ev.payload["mt940"]
    assert parsed.closing_balance_minor == -777_00 + 5_000_00
    assert (
        hashlib.sha256(stmt_ev.payload["mt940"].encode()).hexdigest()
        == hashlib.sha256(write_mt940(parsed).encode()).hexdigest()
    )


def test_tampered_request_amount_is_rejected_with_a_system_error(env: Env) -> None:
    """The frame binds EUR cents; a later payment_requested that claims a different native amount
    for the same pay_id disagrees with the frame and is refused."""
    env.request("PAY-000007", 24_500_00, "CZK", eur=100_000)
    tampered = env.request("PAY-000007", 99_999_99, "CZK", eur=100_000)
    tampered_payload = dict(tampered.payload)
    assert tampered_payload["amount_minor"] == 99_999_99
    frame = env.frame(1, "PAY-000007", 100_000, "CZK")
    res = env.bank.execute(frame, hmac_tag(KEY, frame))
    assert not res.ok and res.reason == "amount_mismatch"
    assert any(
        e.payload["code"] == "amount_mismatch" and "PAY-000007" in e.payload["refs"]
        for e in env.store.query(type="system_error")
    )
    assert env.bank.balance("CZK") == OPENING_BANK_MINOR["CZK"]


def test_native_amount_within_rounding_is_accepted(env: Env) -> None:
    env.request("PAY-000008", 24_500_00, "CZK", eur=100_000)
    frame = env.frame(1, "PAY-000008", 100_000, "CZK")
    assert env.bank.execute(frame, hmac_tag(KEY, frame)).ok
