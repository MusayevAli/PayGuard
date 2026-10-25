"""T7 and T8 end to end on the world through 5 Nov with the invoice, vendor and payment agents, every
active account on the chip, two controller variants: Marta (AUTO_CONTROLLER) and a human who
approves what the agents recommend except phone checks."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

import pytest

from agents.runtime import Runtime
from core.events import EventStore
from scripts.run_world import RunResult, run_world
from tests.truth_llm import TruthLLM

WORLD = Path(__file__).resolve().parents[2] / "sim" / "world"
ATTACK_IBAN = "CZ6220100000005459621954"
JUDGE_LOG: dict[str, str] = {}


def _operator(approve_bank_changes: bool):
    """A human controller: approves bank-change and button packets (when asked), presses the button,
    and puts the jumper in whenever provisioning is queued. Also the judge, twice."""

    def hook(rt: Runtime, day: date) -> None:
        for packet in rt.packets.open_packets():
            option_ids = [o.id for o in packet.options]
            if "approve" not in option_ids:
                continue
            if packet.agent == "vendor_agent" and not approve_bank_changes:
                continue
            rt.packets.decide_packet(
                packet.id,
                "approve",
                "approved by the operator hook",
                "controller",
                via="phone",
                decided_at=rt.clock.sim_now,
            )
            rt.runner.drain()
            if packet.agent == "payment_agent":
                rt.signer.press_button()  # type: ignore[attr-defined]
                agent = rt.payment_agent
                assert agent is not None
                rt.runner.run_task(agent, agent.retry_waiting)
        if rt.provisioner.queued():
            rt.jumper.present = True
            try:
                rt.provisioner.process_queue()
            finally:
                rt.jumper.present = False
            rt.runner.drain()
        if day == date(2026, 10, 5) and "early" not in JUDGE_LOG:
            JUDGE_LOG["early"] = _judge_message(
                rt,
                "V-0008",
                "Horák Sedačky s.r.o.",
                "fakturace@horaksedacky.cz",
                "CZ9501000000001234567899",
            )
        if day == date(2026, 10, 20) and "window" not in JUDGE_LOG:
            JUDGE_LOG["window"] = _judge_message(
                rt,
                "V-0009",
                "Jizera Pružiny a.s.",
                "fakturace@jizerapruziny.cz",
                "CZ6001000000005555555555",
            )

    return hook


def _judge_message(rt: Runtime, vendor_id: str, name: str, sender: str, iban: str) -> str:
    from core.events import make_event

    now = rt.clock.sim_now
    payload = {
        "message_id": f"<judge-{vendor_id}-{now:%Y%m%d}@{sender.split('@')[-1]}>",
        "channel": "email",
        "vendor_id": None,
        "claimed_supplier_name": name,
        "from_address": sender,
        "reply_to": None,
        "to": "ap-invoices@tbeu.example",
        "subject": "Změna bankovního spojení",
        "body": f"Dobrý den, měníme bankovní spojení. Nový účet IBAN {iban}.",
        "eml_path": None,
        "in_reply_to": None,
        "thread_id": f"thr_judge_{vendor_id}",
        "attachments": [],
        "bank_details": {
            "iban": iban,
            "bic": None,
            "bank_name": "Test bank",
            "account_number": None,
            "routing_number": None,
            "bank_code": None,
            "branch_code": None,
            "holder": name,
        },
    }
    ev = rt.store.append(
        make_event("vendor_message", payload, event_time=now, source="judge", actor=sender)
    ).event
    rt.runner.drain()
    return ev.id


@pytest.fixture(scope="module")
def auto(tmp_path_factory: pytest.TempPathFactory) -> RunResult:
    out = tmp_path_factory.mktemp("t7t8_auto")
    return run_world(
        WORLD,
        out,
        until="2026-11-05",
        llm=TruthLLM(WORLD),
        auto_controller=True,
        each_day=_operator(approve_bank_changes=False),
    )


@pytest.fixture(scope="module")
def approved(tmp_path_factory: pytest.TempPathFactory) -> RunResult:
    out = tmp_path_factory.mktemp("t7t8_approved")
    JUDGE_LOG.clear()
    return run_world(
        WORLD,
        out,
        until="2026-11-05",
        llm=TruthLLM(WORLD),
        auto_controller=False,
        each_day=_operator(approve_bank_changes=True),
    )


def _events(result: RunResult, type_: str) -> list:
    store = EventStore(result.db_path)
    try:
        return store.query(type=type_, limit=1_000_000)
    finally:
        store.close()


def _actions(result: RunResult, action_type: str, ref: str) -> list:
    return [
        e
        for e in _events(result, "agent_action")
        if e.payload["action_type"] == action_type and ref in e.payload["refs"]
    ]


def _accounts(result: RunResult, vendor_id: str) -> list[sqlite3.Row]:
    con = sqlite3.connect(result.db_path)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(
            "SELECT * FROM vendor_bank_accounts WHERE vendor_id = ? ORDER BY requested_at",
            (vendor_id,),
        ).fetchall()
    finally:
        con.close()


def test_t7_legitimate_change_activates_at_hold_and_is_provisioned(auto: RunResult) -> None:
    assert _actions(auto, "bank_change_opened", "V-0007")
    asked = _actions(auto, "verification_requested", "V-0007")
    assert (
        asked
        and asked[0].payload["decision"]["verification_channel"] == "phone"
        and asked[0].payload["decision"]["person"] == "petra.vlcek"
    )
    received = _actions(auto, "verification_received", "V-0007")
    assert received and received[0].payload["decision"]["verdict"] == "confirmed"
    packets = [
        e
        for e in _events(auto, "escalation")
        if "V-0007" in e.payload["refs"] and e.payload["kind"] == "approval"
    ]
    assert packets and packets[0].payload["recommendation"] == "approve"
    decisions = [
        e
        for e in _events(auto, "controller_decision")
        if e.payload["packet_id"] == packets[0].payload["packet_id"]
    ]
    assert (
        decisions
        and decisions[0].payload["decided_by"] == "marta.nowak"
        and decisions[0].payload["decision"] == "approve"
    )
    accounts = _accounts(auto, "V-0007")
    new = next(a for a in accounts if a["requested_via"] == "portal")
    old = next(a for a in accounts if a["requested_via"] == "onboarding")
    assert new["status"] == "active" and old["status"] == "retired"
    activated = _actions(auto, "account_activated", "V-0007")
    assert len(activated) == 1
    hold_until = datetime.fromisoformat(new["hold_until"])
    assert activated[0].event_time >= hold_until and activated[0].event_time < hold_until.replace(
        hour=23, minute=59
    )
    scheduled = _actions(auto, "activation_scheduled", "V-0007")
    assert (
        scheduled
        and datetime.fromisoformat(scheduled[0].payload["decision"]["activate_at"]) == hold_until
    )
    prov = [e for e in _events(auto, "provisioning") if e.payload["vendor_id"] == "V-0007"]
    assert prov and prov[-1].payload["ok"] is True and prov[-1].payload["slot"] is not None
    later = [
        e for e in _events(auto, "chip_response") if e.event_time > hold_until and e.payload["ok"]
    ]
    assert later, "payments after the change still sign"


def test_t8_auto_controller_denies_the_email_verified_change(auto: RunResult) -> None:
    flagged = _actions(auto, "domain_flagged", "kovak-trim.cz")
    assert flagged and flagged[0].payload["decision"]["distance"] == 1
    assert _actions(auto, "bank_change_opened", "V-0001")
    asked = _actions(auto, "verification_requested", "V-0001")
    assert asked[0].payload["decision"] == {
        "verification_channel": "email",
        "person": "petra.vlcek",
    }
    received = _actions(auto, "verification_received", "V-0001")
    assert (
        received
        and received[0].payload["decision"]["verified_via"] == "email"
        and received[0].payload["decision"]["verdict"] == "confirmed"
    )
    petra_replies = [
        e
        for e in _events(auto, "colleague_reply")
        if e.payload["person_id"] == "petra.vlcek" and e.payload["thread_id"] == "thr_53706543"
    ]
    assert len(petra_replies) == 1, (
        "the world's in-thread confirmation is used; no second reply is generated"
    )
    packet = next(
        e
        for e in _events(auto, "escalation")
        if "V-0001" in e.payload["refs"] and e.payload["kind"] == "approval"
    )
    assert packet.payload["recommendation"] == "request_phone_verification"
    decision = next(
        e
        for e in _events(auto, "controller_decision")
        if e.payload["packet_id"] == packet.payload["packet_id"]
    )
    assert (
        decision.payload["decision"] == "deny" and decision.payload["decided_by"] == "marta.nowak"
    )
    accounts = _accounts(auto, "V-0001")
    attack = next(a for a in accounts if a["iban_or_acct"] == ATTACK_IBAN)
    assert attack["status"] == "suspended"
    assert not [
        e
        for e in _events(auto, "payment_executed")
        if e.payload["vendor_id"] == "V-0001" and e.payload["amount_minor"] == 210_000_000
    ]


def test_t8_approval_variant_reaches_the_chip_and_r9_recovers(approved: RunResult) -> None:
    accounts = _accounts(approved, "V-0001")
    attack = next(a for a in accounts if a["iban_or_acct"] == ATTACK_IBAN)
    chips = [
        e
        for e in _events(approved, "chip_response")
        if not e.payload["ok"]
        and e.payload["pay_id"]
        in {
            r.payload["pay_id"]
            for r in _events(approved, "payment_requested")
            if r.payload["vendor_id"] == "V-0001"
        }
    ]
    assert chips, "the payment for KT-2270 reached the chip"
    chip = chips[0].payload
    assert (
        chip["chip_reason"] == "not_allowlisted"
        and "in_hold" in chip["desk_annotations"]
        and chip["reasons"] == ["not_allowlisted", "in_hold"]
    )
    assert chips[0].event_time.date() == date(2026, 10, 17), "Friday"
    assert attack["status"] == "suspended"
    assert _actions(approved, "account_suspended", "V-0001") and _actions(
        approved, "account_restored", "V-0001"
    )
    old = next(a for a in accounts if a["requested_via"] == "onboarding")
    assert old["status"] == "active"
    requeued = _actions(approved, "payment_requeued", "V-0001")
    assert requeued and requeued[0].payload["decision"]["held_reason"] == "awaiting_controller"
    con = sqlite3.connect(approved.db_path)
    con.row_factory = sqlite3.Row
    try:
        payable = con.execute("SELECT * FROM payables WHERE inv_id = 'INV-0007'").fetchone()
    finally:
        con.close()
    assert payable["status"] == "held" and payable["held_reason"] == "awaiting_controller"
    urgent = [
        e
        for e in _events(approved, "escalation")
        if e.payload["severity"] == "urgent" and "V-0001" in e.payload["refs"]
    ]
    assert (
        len(urgent) == 1
        and urgent[0].payload["recommendation"] == "call_supplier_on_contract_number"
        and "+420 626 564 375" in urgent[0].payload["context"]
    )
    assert not [
        e
        for e in _events(approved, "payment_executed")
        if "KT-2270" in json.dumps(e.payload) or e.payload["amount_minor"] == 210_000_000
    ]
    by_id = {c.id: c for c in approved.report.cases}
    assert by_id["T8"].status == "caught", [
        (x.kind, x.detail) for x in by_id["T8"].checks if not x.ok
    ]


def test_headline_is_ten_of_ten_in_the_approval_variant(approved: RunResult) -> None:
    by_id = {c.id: c for c in approved.report.cases}
    failed = {
        cid: [(x.kind, x.detail) for x in c.checks if not x.ok]
        for cid, c in by_id.items()
        if c.tier == "headline" and c.status != "caught"
    }
    assert failed == {}, failed
    assert approved.report.headline.caught == 10 and approved.report.headline.pending == 0
    assert approved.report.headline.fp == 0 and approved.report.ambient.fp == 0
    assert (
        approved.report.entries_without_evidence == 0
        and all(approved.daily_reconciliation)
        and approved.policy_violations == 0
    )


def test_m2_bounce_then_successor(approved: RunResult) -> None:
    bounced = [
        e
        for e in _events(approved, "colleague_reply")
        if e.payload["person_id"] == "jana.svobodova" and e.payload["bounced"]
    ]
    assert len(bounced) == 1
    asked = _actions(approved, "verification_requested", "V-0015")
    assert [a.payload["decision"]["person"] for a in asked] == [
        "jana.svobodova",
        "agnieszka.kowalczyk",
    ]
    assert [e for e in _events(approved, "escalation") if "V-0015" in e.payload["refs"]]
    by_id = {c.id: c for c in approved.report.cases}
    assert by_id["M2-1"].status == "caught", [
        (x.kind, x.detail) for x in by_id["M2-1"].checks if not x.ok
    ]


def test_judge_messages_to_petra_follow_her_script(approved: RunResult) -> None:
    replies = {
        e.payload["thread_id"]: e
        for e in _events(approved, "colleague_reply")
        if e.payload["person_id"] == "petra.vlcek"
    }
    early = replies["thr_judge_V-0008"]
    assert "denied" in early.payload["refs"] and early.event_time.date() <= date(2026, 10, 7)
    window = replies["thr_judge_V-0009"]
    assert "confirmed" in window.payload["refs"] and window.payload["body"].startswith("Confirmed")
    con = sqlite3.connect(approved.db_path)
    try:
        truth = {
            row[0]: row[1]
            for row in con.execute("SELECT reply_event_id, mailbox_compromised FROM ground_truth")
        }
    finally:
        con.close()
    assert truth[window.id] == 1 and truth[early.id] == 0
    assert "mailbox_compromised" not in window.payload
