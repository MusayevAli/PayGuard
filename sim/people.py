"""Model-played people (spec §2.8): the roster and their hidden scripts.

Part 1 provides the data; ``reply()`` (LLM-played) arrives in Part 5. Agents only ever see
``id``, ``name``, ``role``, ``email`` and ``phone``; the compromise window and scripts are
ground truth for the simulator and the scoreboard.
"""

from __future__ import annotations

from datetime import date

from sim.models import Person, PersonScript

COMPANY_DOMAIN = "tbeu.example"

PETRA_CONFIRMATION = "Confirmed, they told me last week."


def roster() -> list[Person]:
    return [
        Person(
            id="petra.vlcek",
            name="Petra Vlček",
            role="purchasing",
            email=f"petra.vlcek@{COMPANY_DOMAIN}",
            phone="+420 485 100 001",
            plant="CZ",
            mailbox_compromised_from=date(2026, 10, 14),
            active=True,
            left_at=None,
            persona=(
                "Purchasing lead for the Czech supplier base. Direct, helpful, answers in one or two "
                "sentences, signs 'Petra'. Trusts suppliers she has worked with for years."
            ),
            script=PersonScript(
                email_reply_when_compromised=PETRA_CONFIRMATION,
                phone_reply=(
                    "No. Nobody at Kovak Trim told me about any bank change, and I have not written "
                    "anything confirming one. Do not change the account."
                ),
                reply_style="short, warm, Czech-accented English",
                confirms_bank_change_for=["V-0007"],
            ),
        ),
        Person(
            id="jonas.weber",
            name="Jonas Weber",
            role="warehouse",
            email=f"jonas.weber@{COMPANY_DOMAIN}",
            phone="+49 89 5500 2210",
            plant="DE",
            mailbox_compromised_from=None,
            active=True,
            left_at=None,
            persona=(
                "Dock lead in Munich. Replies in short Czech-inflected English ('is booked now', "
                "'we have 800 piece, rest come next week'). Looks things up before answering, slowly."
            ),
            script=PersonScript(
                delayed_replies={"HM-09": 30},
                wrong_po_answer=None,  # filled by the generator with the M5 case it plants
                reply_style="short, Czech-inflected English, no greetings",
            ),
        ),
        Person(
            id="marta.nowak",
            name="Marta Nowak",
            role="controller",
            email=f"marta.nowak@{COMPANY_DOMAIN}",
            phone="+48 32 230 1100",
            plant="PL",
            mailbox_compromised_from=None,
            active=True,
            left_at=None,
            persona=(
                "Group controller. Used only when no human is on the phone. Precise, polite, "
                "refuses anything whose evidence chain is incomplete and says exactly what is missing."
            ),
            script=PersonScript(
                denies_incomplete_evidence=True,
                reply_style="formal, numbered reasons",
            ),
        ),
        Person(
            id="tomas.horak",
            name="Tomáš Horák",
            role="buyer",
            email=f"tomas.horak@{COMPANY_DOMAIN}",
            phone="+420 485 100 014",
            plant="CZ",
            mailbox_compromised_from=None,
            active=True,
            left_at=None,
            persona=(
                "Buyer responsible for freight and tooling. Confirms freight invoices he ordered, "
                "asks for the PO or shipment reference when he did not."
            ),
            script=PersonScript(
                confirms_freight_up_to_minor=1_500_000,
                reply_style="brisk, bullet points",
            ),
        ),
        Person(
            id="lukas.brandt",
            name="Lukas Brandt",
            role="purchasing",
            email=f"lukas.brandt@{COMPANY_DOMAIN}",
            phone="+49 89 5500 2140",
            plant="DE",
            mailbox_compromised_from=None,
            active=True,
            left_at=None,
            persona="Buyer for German, Austrian and overseas suppliers. Precise, answers with PO numbers.",
            script=PersonScript(reply_style="concise German-accented English"),
        ),
        Person(
            id="agnieszka.kowalczyk",
            name="Agnieszka Kowalczyk",
            role="purchasing",
            email=f"agnieszka.kowalczyk@{COMPANY_DOMAIN}",
            phone="+48 32 230 1122",
            plant="PL",
            mailbox_compromised_from=None,
            active=True,
            left_at=None,
            persona="Buyer for Polish suppliers; took over Jana Svobodová's portfolio in September 2026.",
            script=PersonScript(
                reply_style="friendly, thorough", confirms_bank_change_for=["V-0015"]
            ),
        ),
        Person(
            id="jana.svobodova",
            name="Jana Svobodová",
            role="purchasing",
            email=f"jana.svobodova@{COMPANY_DOMAIN}",
            phone="+48 32 230 1157",
            plant="PL",
            mailbox_compromised_from=None,
            active=False,
            left_at=date(2026, 8, 31),
            persona="Former buyer for Polish suppliers. Left the company on 31 August 2026; mail bounces.",
            script=PersonScript(
                reply_style="bounce: 550 5.1.1 user unknown", successor_id="agnieszka.kowalczyk"
            ),
        ),
    ]


def by_id() -> dict[str, Person]:
    return {p.id: p for p in roster()}


# ---------------------------------------------------------------------------------------
# The people layer (Part 3 scripted responders, Part 5 verification replies and the controller).
# Facts come from hidden scripts; wording comes from a persona model when a key is configured and
# from per-person templates otherwise. Either way the event shape is identical.
# ---------------------------------------------------------------------------------------

import random  # noqa: E402
import re  # noqa: E402
import sqlite3  # noqa: E402
import threading  # noqa: E402
from collections.abc import Callable, Sequence  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

from pydantic import BaseModel, ConfigDict  # noqa: E402

from core.events import make_event  # noqa: E402
from sim.clock import SimClock  # noqa: E402
from sim.models import ColleagueQuery, Event, Supplier  # noqa: E402

DEFAULT_WAREHOUSE_DELAY_HOURS = 4.0
DEFAULT_BUYER_DELAY_HOURS = 6.0
REPLY_DELAY_HOURS: dict[str, tuple[float, float]] = {
    "email": (2.0, 30.0),
    "phone": (1.0, 3.0),
    "portal": (6.0, 12.0),
    "chat": (0.5, 4.0),
}


class ScriptedReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str
    delay_hours: float
    refs: list[str]


class Reply(BaseModel):
    """A person's answer: the scripted facts (``refs``, ``facts``) and the wording (``body``)."""

    model_config = ConfigDict(extra="forbid")
    person_id: str
    channel: str
    body: str
    delay_hours: float
    refs: list[str]
    bounced: bool = False
    facts: dict[str, Any] = {}
    wording: str = "template"


class ScriptedPerson:
    """A person with a hidden script who answers agent queries from what the warehouse/ERP shows."""

    def __init__(self, person: Person, events_source: Callable[[], Sequence[Event]]) -> None:
        self.person = person
        self._events = events_source

    def reply(self, query: ColleagueQuery, sim_time: datetime) -> ScriptedReply | None:
        if not self.person.active:
            return ScriptedReply(
                body=f"Mail delivery failed: {self.person.email} (550 5.1.1 user unknown)",
                delay_hours=0.1,
                refs=["bounced"],
            )
        if query.kind == "warehouse_query":
            return self._warehouse(query, sim_time)
        if query.kind == "buyer_confirmation":
            return self._buyer(query)
        return None

    # -- warehouse (Jonas) ----------------------------------------------------------------
    def _warehouse(self, query: ColleagueQuery, sim_time: datetime) -> ScriptedReply:
        script = self.person.script
        number = query.invoice_number or ""
        delay = float(script.delayed_replies.get(number, DEFAULT_WAREHOUSE_DELAY_HOURS))
        reply_time = sim_time + timedelta(hours=delay)
        wrong = script.wrong_po_answer
        if wrong is not None and wrong.invoice_number == number:
            return ScriptedReply(
                body=f"We have {wrong.answers_qty} pc on {wrong.answers_from_po}, that is what I see here.",
                delay_hours=delay,
                refs=[f"qty:{wrong.answers_qty}", f"po:{wrong.answers_from_po}"],
            )
        po_line = self._po_line(query.po_id, query.line_no)
        receipts = [
            e.payload["receipt"]
            for e in self._events()
            if e.type == "goods_received"
            and e.payload["receipt"]["po_id"] == query.po_id
            and e.known_at <= reply_time
        ]
        on_line = [r for r in receipts if r["line_no"] == query.line_no]
        if on_line:
            r = sorted(on_line, key=lambda x: x["booked_at"])[-1]
            refs = [
                r["id"],
                f"qty:{r['qty']}",
                f"uom:{r['uom']}",
                f"line:{r['line_no']}",
                "confirmed",
            ]
            if po_line is not None and r["uom"] != po_line["uom"]:
                refs = [
                    r["id"],
                    f"qty:{r['qty']}",
                    f"uom:{po_line['uom']}",
                    f"line:{r['line_no']}",
                    "unit_corrected",
                    "confirmed",
                ]
                return ScriptedReply(
                    body=f"{r['id']} is {r['qty']} {po_line['uom']}, I picked {r['uom']} by mistake. Is booked now, sorry.",
                    delay_hours=delay,
                    refs=refs,
                )
            return ScriptedReply(
                body=f"Is booked: {r['id']}, {r['qty']} {r['uom']} on line {r['line_no']} of {query.po_id}.",
                delay_hours=delay,
                refs=refs,
            )
        others = [r for r in receipts if r["line_no"] != query.line_no]
        if others:
            r = sorted(others, key=lambda x: x["booked_at"])[-1]
            return ScriptedReply(
                body=f"{r['id']} is booked on line {r['line_no']} but it is the {query.sku or 'invoiced'} delivery, {r['qty']} {r['uom']}. Please move it to line {query.line_no}.",
                delay_hours=delay,
                refs=[
                    r["id"],
                    f"qty:{r['qty']}",
                    f"uom:{r['uom']}",
                    f"line:{query.line_no}",
                    "reassign",
                    "confirmed",
                ],
            )
        return ScriptedReply(
            body=f"Nothing received for {query.po_id} line {query.line_no} yet, I check again tomorrow.",
            delay_hours=delay,
            refs=["nothing_received"],
        )

    def _po_line(self, po_id: str | None, line_no: int | None) -> dict[str, Any] | None:
        if po_id is None or line_no is None:
            return None
        for e in self._events():
            if e.type == "po_created" and e.payload["po"]["id"] == po_id:
                for ln in e.payload["po"]["lines"]:
                    if ln["line_no"] == line_no:
                        return dict(ln)
        return None

    # -- buyer (Tomáš) --------------------------------------------------------------------
    def _buyer(self, query: ColleagueQuery) -> ScriptedReply:
        limit = self.person.script.confirms_freight_up_to_minor
        amount = query.amount_minor or 0
        if limit is not None and amount <= limit:
            return ScriptedReply(
                body=f"Confirmed, that was my shipment ({query.invoice_number}). Post it to freight.",
                delay_hours=DEFAULT_BUYER_DELAY_HOURS,
                refs=["confirmed"],
            )
        return ScriptedReply(
            body=f"Not mine. I did not order {query.invoice_number}; please check with the plant.",
            delay_hours=DEFAULT_BUYER_DELAY_HOURS,
            refs=["not_mine"],
        )


class GroundTruth:
    """What the agents must never read: which replies came from a compromised mailbox (spec §7.6)."""

    def __init__(self, path: str | Path) -> None:
        self._lock = threading.RLock()
        self._con = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS ground_truth (reply_event_id TEXT PRIMARY KEY, person_id TEXT NOT NULL, mailbox_compromised INTEGER NOT NULL, note TEXT NOT NULL)"
        )

    def close(self) -> None:
        with self._lock:
            self._con.close()

    def record(
        self, reply_event_id: str, person_id: str, *, mailbox_compromised: bool, note: str
    ) -> None:
        with self._lock:
            self._con.execute(
                "INSERT OR IGNORE INTO ground_truth(reply_event_id, person_id, mailbox_compromised, note) VALUES (?,?,?,?)",
                (reply_event_id, person_id, 1 if mailbox_compromised else 0, note),
            )

    def rows(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(r)
                for r in self._con.execute("SELECT * FROM ground_truth ORDER BY rowid").fetchall()
            ]


def _mask(account: str | None) -> str:
    if not account:
        return "****"
    compact = account.replace(" ", "")
    return f"{compact[:4]} **** {compact[-4:]}" if len(compact) > 8 else "****"


def _first_name(person: Person) -> str:
    return person.name.split()[0]


class People:
    """Routes agent questions to people, decides the scripted facts, words the reply and delivers
    it as a ``colleague_reply`` through the clock. Also the Part 3 ``Messenger``."""

    def __init__(
        self,
        people: Sequence[Person],
        *,
        events_source: Callable[[], Sequence[Event]],
        clock: SimClock,
        store: Any,
        suppliers: Sequence[Supplier] = (),
        truth_store: GroundTruth | None = None,
        llm: Any | None = None,
        seed: int = 1,
    ) -> None:
        self._people = {p.id: p for p in people}
        self._scripted = {p.id: ScriptedPerson(p, events_source) for p in people}
        self._events = events_source
        self._clock = clock
        self._store = store
        self._suppliers = {s.id: s for s in suppliers}
        self._truth = truth_store
        self._llm = llm
        self._seed = seed

    # ------------------------------------------------------------------ roster

    def person(self, person_id: str) -> Person:
        return self._people[person_id]

    def has(self, person_id: str) -> bool:
        return person_id in self._people

    def successor(self, person_id: str) -> Person | None:
        p = self._people.get(person_id)
        if p is None or p.script.successor_id is None:
            return None
        return self._people.get(p.script.successor_id)

    def legit_bank_changes(self) -> set[str]:
        return {v for p in self._people.values() for v in p.script.confirms_bank_change_for}

    # ------------------------------------------------------------------ Part 5: verification replies

    def reply(
        self,
        to_person: str,
        channel: str,
        message: str,
        sim_time: datetime,
        *,
        context: dict[str, Any] | None = None,
    ) -> Reply:
        """What ``to_person`` answers over ``channel``. Facts follow the hidden script; wording is
        the persona model's when available, otherwise the person's templates."""
        person = self._people[to_person]
        ctx = dict(context or {})
        thread = str(ctx.get("thread_id") or message[:40])
        delay = self._delay(person.id, channel, thread)
        if not person.active:
            return Reply(
                person_id=person.id,
                channel=channel,
                body=f"Mail delivery failed: {person.email} (550 5.1.1 user unknown)",
                delay_hours=0.1,
                refs=["bounced"],
                bounced=True,
                facts={"bounced": True},
            )
        compromised = (
            channel == "email"
            and person.mailbox_compromised_from is not None
            and sim_time.date() >= person.mailbox_compromised_from
        )
        kind = str(ctx.get("kind") or "generic")
        if kind == "bank_change_verification":
            vendor_id = str(ctx.get("vendor_id") or "")
            vendor_name = str(ctx.get("vendor_name") or vendor_id)
            supplier = self._suppliers.get(vendor_id)
            on_file = _mask(supplier.iban if supplier else None)
            phone = supplier.phone if supplier else "the contract number"
            if compromised:
                verdict, template = (
                    "confirmed",
                    person.script.email_reply_when_compromised
                    or "Confirmed, they told me last week.",
                )
            elif vendor_id in person.script.confirms_bank_change_for:
                verdict, template = (
                    "confirmed",
                    f"Yes, {vendor_name} told us about the new account; it is fine to update it. {_first_name(person)}",
                )
            elif person.id == "petra.vlcek" and person.script.phone_reply and vendor_id == "V-0001":
                verdict, template = "denied", person.script.phone_reply
            else:
                verdict, template = (
                    "denied",
                    (
                        f"No. Nobody at {vendor_name} told me about a bank change. The account on file is {on_file}; "
                        f"if in doubt call them on the contract number {phone}. {_first_name(person)}"
                    ),
                )
            facts = {
                "verdict": verdict,
                "vendor_id": vendor_id,
                "contract_phone": phone,
                "account_on_file_masked": on_file,
            }
            refs = [verdict, vendor_id] if vendor_id else [verdict]
        elif kind == "onboarding_verification":
            vendor_name = str(ctx.get("vendor_name") or "")
            known = any(
                vendor_name.lower() in (s.legal_name.lower(), *[v.lower() for v in s.name_variants])
                for s in self._suppliers.values()
            )
            if known:
                verdict, template = (
                    "confirmed",
                    f"Yes, we buy from {vendor_name}; the invoice looks right to me. {_first_name(person)}",
                )
            else:
                verdict, template = (
                    "denied",
                    f"I do not know {vendor_name} and I have not ordered anything from them. Please do not pay until purchasing has onboarded them. {_first_name(person)}",
                )
            facts = {"verdict": verdict, "vendor_name": vendor_name}
            refs = [verdict]
        else:
            verdict, template = "acknowledged", f"Noted, thank you. {_first_name(person)}"
            facts = {"verdict": verdict}
            refs = []
        body, wording = self._word(person, channel, message, template, {**ctx, "facts": facts})
        return Reply(
            person_id=person.id,
            channel=channel,
            body=body,
            delay_hours=delay,
            refs=refs,
            bounced=False,
            facts={**facts, "mailbox_compromised": compromised},
            wording=wording,
        )

    def deliver(
        self,
        reply: Reply,
        *,
        in_reply_to: str,
        thread_id: str | None,
        sim_time: datetime,
        refs: Sequence[str] = (),
        subject: str | None = None,
    ) -> Event | None:
        """Schedule the ``colleague_reply`` on the clock. When the world already carries this
        person's reply in the same thread (T8's in-thread confirmation), nothing is scheduled."""
        if thread_id and self._world_reply_exists(reply.person_id, thread_id, after=sim_time):
            return None
        when = sim_time + timedelta(hours=reply.delay_hours)
        person = self._people[reply.person_id]
        payload = {
            "message_id": f"<{when:%Y%m%d%H%M%S}.{re.sub(r'[^0-9a-f]', '', in_reply_to)[:12]}@{COMPANY_DOMAIN}>",
            "person_id": person.id,
            "channel": reply.channel
            if reply.channel in ("email", "phone", "chat", "portal")
            else "chat",
            "in_reply_to": in_reply_to,
            "thread_id": thread_id or in_reply_to,
            "subject": subject,
            "body": reply.body,
            "eml_path": None,
            "refs": list(dict.fromkeys([*refs, *reply.refs])),
            "bounced": reply.bounced,
        }
        ev = make_event(
            "colleague_reply", payload, event_time=when, source="people", actor=person.email
        )
        self._clock.schedule(ev)
        if self._truth is not None:
            self._truth.record(
                ev.id,
                person.id,
                mailbox_compromised=bool(reply.facts.get("mailbox_compromised")),
                note=reply.facts.get("verdict", ""),
            )
        return ev

    def _world_reply_exists(self, person_id: str, thread_id: str, *, after: datetime) -> bool:
        """A reply by this person in this thread that the world already carries (delivered or still
        pending) stands in for anything the runtime would generate."""
        del after
        for e in self._events():
            if (
                e.type == "colleague_reply"
                and e.payload.get("person_id") == person_id
                and e.payload.get("thread_id") == thread_id
            ):
                return True
        return False

    def _delay(self, person_id: str, channel: str, thread: str) -> float:
        lo, hi = REPLY_DELAY_HOURS.get(channel, (1.0, 4.0))
        return round(
            random.Random(f"{self._seed}:{person_id}:{channel}:{thread}").uniform(lo, hi), 1
        )

    def _word(
        self, person: Person, channel: str, message: str, template: str, context: dict[str, Any]
    ) -> tuple[str, str]:
        persona = getattr(self._llm, "persona_reply", None)
        if callable(persona):
            text = persona(
                person.model_dump(mode="json"),
                {"channel": channel, "message": message, "template": template, **context},
            )
            if isinstance(text, str) and text.strip():
                return text.strip(), "persona"
        return template, "template"

    # ------------------------------------------------------------------ Part 3: agent queries

    def ask(self, query: ColleagueQuery, *, asked_event_id: str, sim_time: datetime) -> None:
        responder = self._scripted.get(query.to_person)
        if responder is None:
            return
        scripted = responder.reply(query, sim_time)
        if scripted is None:
            return
        reply = Reply(
            person_id=query.to_person,
            channel="chat",
            body=scripted.body,
            delay_hours=scripted.delay_hours,
            refs=scripted.refs,
            bounced="bounced" in scripted.refs,
            facts={},
        )
        self.deliver(
            reply,
            in_reply_to=asked_event_id,
            thread_id=asked_event_id,
            sim_time=sim_time,
            refs=query.refs,
        )


Responders = People


class AutoController:
    """Marta Nowak deciding packets when no human is on the phone (AUTO_CONTROLLER=1): approves
    only complete evidence (three rows or more) and never a packet that itself recommends a phone
    verification; packets without an approve/deny pair follow their recommendation when complete."""

    principal = "controller"
    subscribes = frozenset({"escalation"})

    def __init__(self, packets: Any, *, person_id: str = "marta.nowak") -> None:
        self._packets = packets
        self._person_id = person_id

    def decide(self, packet: Any) -> tuple[str | None, str]:
        rows = len(packet.evidence)
        option_ids = [o.id for o in packet.options]
        reasons: list[str] = []
        if rows < 3:
            reasons.append(f"1. Evidence has {rows} row(s); I need at least three.")
        if packet.recommendation == "request_phone_verification":
            reasons.append(
                "2. The agent itself recommends a phone verification; approving would skip it."
            )
        if "approve" in option_ids:
            if reasons:
                negative = (
                    "deny"
                    if "deny" in option_ids
                    else ("reject" if "reject" in option_ids else None)
                )
                return negative, " ".join(reasons) or "denied"
            return "approve", "Evidence complete; recommendation followed."
        if reasons:
            return None, " ".join(reasons)
        return packet.recommendation, "Evidence complete; recommendation followed."

    def handle(self, ev: Event, ctx: Any) -> list[Event]:
        packet = self._packets.get(str(ev.payload["packet_id"]))
        if packet is None or packet.decision is not None:
            return []
        decision, note = self.decide(packet)
        if decision is None:
            return []
        self._packets.decide_packet(
            packet.id, decision, note, self._person_id, via="phone", decided_at=ctx.now
        )
        return []
