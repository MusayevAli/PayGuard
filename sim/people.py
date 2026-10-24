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
