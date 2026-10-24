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
            script=PersonScript(reply_style="friendly, thorough"),
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
            script=PersonScript(reply_style="bounce: 550 5.1.1 user unknown"),
        ),
    ]


def by_id() -> dict[str, Person]:
    return {p.id: p for p in roster()}


# ---------------------------------------------------------------------------------------
# Scripted responders (Part 3). Part 5 adds LLM personas behind the same ``reply`` interface.
# ---------------------------------------------------------------------------------------

import re  # noqa: E402
from collections.abc import Callable, Sequence  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402
from typing import Any  # noqa: E402

from pydantic import BaseModel, ConfigDict  # noqa: E402

from core.events import make_event  # noqa: E402
from sim.clock import SimClock  # noqa: E402
from sim.models import ColleagueQuery, Event  # noqa: E402

DEFAULT_WAREHOUSE_DELAY_HOURS = 4.0
DEFAULT_BUYER_DELAY_HOURS = 6.0


class ScriptedReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str
    delay_hours: float
    refs: list[str]


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


class Responders:
    """Messenger implementation: routes a query to the scripted person and schedules the
    ``colleague_reply`` event on the clock."""

    def __init__(
        self,
        people: Sequence[Person],
        *,
        events_source: Callable[[], Sequence[Event]],
        clock: SimClock,
        store: Any,
    ) -> None:
        self._people = {p.id: ScriptedPerson(p, events_source) for p in people}
        self._clock = clock
        self._store = store

    def person(self, person_id: str) -> Person:
        return self._people[person_id].person

    def ask(self, query: ColleagueQuery, *, asked_event_id: str, sim_time: datetime) -> None:
        responder = self._people.get(query.to_person)
        if responder is None:
            return
        reply = responder.reply(query, sim_time)
        if reply is None:
            return
        when = sim_time + timedelta(hours=reply.delay_hours)
        person = responder.person
        payload = {
            "message_id": f"<{when:%Y%m%d%H%M%S}.{re.sub(r'[^0-9a-f]', '', asked_event_id)[:12]}@{COMPANY_DOMAIN}>",
            "person_id": person.id,
            "channel": "chat",
            "in_reply_to": asked_event_id,
            "thread_id": asked_event_id,
            "subject": None,
            "body": reply.body,
            "eml_path": None,
            "refs": list(dict.fromkeys([*query.refs, *reply.refs])),
            "bounced": "bounced" in reply.refs,
        }
        self._clock.schedule(
            make_event(
                "colleague_reply", payload, event_time=when, source="chat", actor=person.email
            )
        )
