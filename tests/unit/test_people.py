"""The people layer (spec §2.8/§7.6): scripted facts, delays per channel, the compromised flag in
the ground-truth table only, and identical event shapes with template or persona wording."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from core.events import EventStore
from sim.clock import SimClock
from sim.models import Event
from sim.people import REPLY_DELAY_HOURS, AutoController, GroundTruth, People, roster
from sim.worldgen.suppliers import build_suppliers

OCT_5 = datetime(2026, 10, 5, 9, tzinfo=UTC)
OCT_20 = datetime(2026, 10, 20, 9, tzinfo=UTC)
CTX = {
    "kind": "bank_change_verification",
    "vendor_id": "V-0001",
    "vendor_name": "Kovak Trim s.r.o.",
    "thread_id": "thr_1",
}


def make(tmp_path: Path, llm=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = EventStore(tmp_path / "pg.db")
    clock = SimClock(store, [], start=OCT_5, speed=1)
    truth = GroundTruth(tmp_path / "pg.db")
    people = People(
        roster(),
        events_source=lambda: list(clock.pending_events()),
        clock=clock,
        store=store,
        suppliers=build_suppliers(42),
        truth_store=truth,
        llm=llm,
    )
    return store, clock, truth, people


def test_petra_email_in_the_window_confirms_and_is_recorded_as_compromised(tmp_path: Path) -> None:
    _store, _clock, truth, people = make(tmp_path)
    reply = people.reply(
        "petra.vlcek", "email", "Did Kovak Trim change their account?", OCT_20, context=CTX
    )
    assert (
        reply.refs == ["confirmed", "V-0001"]
        and reply.facts["verdict"] == "confirmed"
        and reply.facts["mailbox_compromised"] is True
    )
    assert reply.body == "Confirmed, they told me last week."
    ev = people.deliver(
        reply, in_reply_to="ev_ask", thread_id="thr_1", sim_time=OCT_20, refs=["V-0001"]
    )
    assert ev is not None and ev.type == "colleague_reply" and ev.payload["channel"] == "email"
    assert "mailbox_compromised" not in ev.payload and "compromised" not in str(ev.payload).lower()
    assert (
        truth.rows()[0]["reply_event_id"] == ev.id and truth.rows()[0]["mailbox_compromised"] == 1
    )
    assert (
        ev.known_at == OCT_20.replace(hour=9) + (ev.known_at - OCT_20)
        and 2 <= reply.delay_hours <= 30
    )


def test_petra_phone_and_pre_window_email_tell_the_truth(tmp_path: Path) -> None:
    _store, _clock, _truth, people = make(tmp_path)
    phone = people.reply(
        "petra.vlcek", "phone", "Did Kovak Trim change their account?", OCT_20, context=CTX
    )
    assert (
        phone.refs == ["denied", "V-0001"]
        and "Do not change the account" in phone.body
        and phone.facts["mailbox_compromised"] is False
    )
    assert phone.facts["contract_phone"].startswith("+420") and phone.facts[
        "account_on_file_masked"
    ].startswith("CZ67")
    early = people.reply(
        "petra.vlcek", "email", "Did Kovak Trim change their account?", OCT_5, context=CTX
    )
    assert early.refs[0] == "denied" and early.facts["mailbox_compromised"] is False
    legit = people.reply(
        "petra.vlcek",
        "phone",
        "Liberec Foam changed their account?",
        OCT_5,
        context={**CTX, "vendor_id": "V-0007", "vendor_name": "Liberec Foam a.s."},
    )
    assert legit.refs == ["confirmed", "V-0007"]


def test_delays_follow_the_channel_and_are_deterministic(tmp_path: Path) -> None:
    _store, _clock, _truth, people = make(tmp_path)
    for channel, (lo, hi) in REPLY_DELAY_HOURS.items():
        if channel == "chat":
            continue
        r1 = people.reply(
            "agnieszka.kowalczyk", channel, "q", OCT_5, context={**CTX, "thread_id": "t"}
        )
        r2 = people.reply(
            "agnieszka.kowalczyk", channel, "q", OCT_5, context={**CTX, "thread_id": "t"}
        )
        assert lo <= r1.delay_hours <= hi and r1.delay_hours == r2.delay_hours


def test_inactive_person_bounces_and_has_a_successor(tmp_path: Path) -> None:
    _store, _clock, _truth, people = make(tmp_path)
    reply = people.reply("jana.svobodova", "email", "q", OCT_5, context=CTX)
    assert reply.bounced and reply.refs == ["bounced"] and reply.delay_hours < 1
    assert (
        people.successor("jana.svobodova") is not None
        and people.successor("jana.svobodova").id == "agnieszka.kowalczyk"
    )  # type: ignore[union-attr]


def test_world_reply_in_the_thread_suppresses_a_second_delivery(tmp_path: Path) -> None:
    _store, clock, _truth, people = make(tmp_path)
    world_reply = Event(
        id="ev_000485",
        type="colleague_reply",
        event_time=OCT_20,
        known_at=OCT_20.replace(hour=11),
        source="email",
        actor="petra",
        payload={
            "message_id": "<m@x>",
            "person_id": "petra.vlcek",
            "channel": "email",
            "in_reply_to": None,
            "thread_id": "thr_53706543",
            "subject": None,
            "body": "Confirmed",
            "eml_path": None,
            "refs": [],
            "bounced": False,
        },
    )
    clock.schedule(world_reply)
    reply = people.reply(
        "petra.vlcek", "email", "q", OCT_20, context={**CTX, "thread_id": "thr_53706543"}
    )
    assert (
        people.deliver(reply, in_reply_to="ev_ask", thread_id="thr_53706543", sim_time=OCT_20)
        is None
    )
    assert (
        people.deliver(reply, in_reply_to="ev_ask", thread_id="thr_other", sim_time=OCT_20)
        is not None
    )


class PersonaLLM:
    def persona_reply(self, person: dict, context: dict) -> str:
        return f"[{person['name']} speaking] {context['template']} -- {context['facts']['verdict']}"


def test_persona_wording_keeps_the_same_facts_and_event_shape(tmp_path: Path) -> None:
    _s1, _c1, _t1, templated = make(tmp_path / "a")
    _s2, _c2, _t2, worded = make(tmp_path / "b", llm=PersonaLLM())
    a = templated.reply("petra.vlcek", "email", "q", OCT_20, context=CTX)
    b = worded.reply("petra.vlcek", "email", "q", OCT_20, context=CTX)
    assert a.body != b.body and b.wording == "persona" and a.wording == "template"
    assert a.refs == b.refs and a.facts == b.facts and a.delay_hours == b.delay_hours
    ea = templated.deliver(a, in_reply_to="ev_ask", thread_id="thr_1", sim_time=OCT_20)
    eb = worded.deliver(b, in_reply_to="ev_ask", thread_id="thr_1", sim_time=OCT_20)
    assert ea is not None and eb is not None
    assert set(ea.payload) == set(eb.payload) and {
        k: v for k, v in ea.payload.items() if k not in ("body", "message_id")
    } == {k: v for k, v in eb.payload.items() if k not in ("body", "message_id")}


def test_marta_rules(tmp_path: Path) -> None:
    from core.packets import PacketDraft, PacketEvidence, PacketOption, PacketStore

    store = EventStore(tmp_path / "pg.db")
    packets = PacketStore(tmp_path / "pg.db", events=store, now=lambda: OCT_5)
    marta = AutoController(packets)

    def packet(rows: int, recommendation: str, options: list[str]):
        return packets.create_packet(
            PacketDraft(
                agent="vendor_agent",
                question=f"q{rows}{recommendation}",
                context="c",
                severity="action",
                refs=["V-0001"],
                evidence=[
                    PacketEvidence(event_id=f"e{i}", role="message", summary=f"row {i}")
                    for i in range(rows)
                ],
                materiality_minor=1,
                currency="EUR",
                recommendation=recommendation,
                recommendation_reason="r",
                options=[PacketOption(id=o, label=o) for o in options],
                expires_at=None,
                consequence_of_silence="x",
                kind="approval",
            ),
            created_at=OCT_5,
        )

    assert (
        marta.decide(packet(4, "approve", ["approve", "deny", "request_phone_verification"]))[0]
        == "approve"
    )
    assert marta.decide(packet(2, "approve", ["approve", "deny"]))[0] == "deny"
    assert (
        marta.decide(
            packet(
                4, "request_phone_verification", ["approve", "deny", "request_phone_verification"]
            )
        )[0]
        == "deny"
    )
    assert (
        marta.decide(
            packet(
                3, "draw_intercompany", ["pay_partial_now", "draw_intercompany", "skip_discount"]
            )
        )[0]
        == "draw_intercompany"
    )
    assert (
        marta.decide(
            packet(
                0, "draw_intercompany", ["pay_partial_now", "draw_intercompany", "skip_discount"]
            )
        )[0]
        is None
    )
    assert date(2026, 10, 14) == roster()[0].mailbox_compromised_from
    packets.close()
    store.close()
