"""One place that wires the desk together (used by ``scripts/run_world.py`` and the API): event
store, clock, ledger, vendor master, packets, policy, agent state, LLM boundary, signer, bank,
provisioning, the agents, the runner and the scoreboard."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from agents.invoice_agent import InvoiceAgent
from agents.llm import LLMClient
from agents.payment_agent import PaymentAgent
from agents.payment_state import PaymentState
from agents.runner import Runner
from agents.state import AgentState
from agents.vendor_agent import VendorAgent
from agents.vendor_state import VendorState
from bank.mock_bank import MockBank
from core.bootstrap import BootstrapResult, bootstrap
from core.events import EventStore
from core.ledger import Ledger
from core.packets import PacketStore
from core.policy import Policy
from core.score import Scoreboard, scoreboard_from_world
from core.vendors import VendorMaster
from signer.interface import Signer
from signer.provisioning import Provisioner, chip_key
from signer.stub import StubSigner
from sim.clock import SimClock
from sim.fx import FxTable, load_fx_rates
from sim.models import Event, Person, Supplier
from sim.people import AutoController, People
from sim.people import GroundTruth as PeopleTruth

log = structlog.get_logger("runtime")


class JumperFlag:
    """The provisioning jumper until the hardware client lands (Part 7)."""

    def __init__(self, present: bool = False) -> None:
        self.present = present

    def __call__(self) -> bool:
        return self.present


@dataclass
class Runtime:
    world_dir: Path
    db_path: Path
    out_dir: Path
    store: EventStore
    clock: SimClock
    fx: FxTable
    ledger: Ledger
    vendors: VendorMaster
    packets: PacketStore
    policy: Policy
    invoice_state: AgentState
    payment_state: PaymentState
    vendor_state: VendorState
    people_truth: PeopleTruth
    llm: Any
    signer: Signer
    bank: MockBank
    provisioner: Provisioner
    jumper: JumperFlag
    invoice_agent: InvoiceAgent | None
    payment_agent: PaymentAgent | None
    vendor_agent: VendorAgent | None
    auto_controller: AutoController | None
    runner: Runner
    scoreboard: Scoreboard | None
    suppliers: list[Supplier]
    people: list[Person]
    responders: People
    owns_llm: bool

    def close(self) -> None:
        for closer in (
            self.ledger.close,
            self.vendors.close,
            self.packets.close,
            self.invoice_state.close,
            self.payment_state.close,
            self.vendor_state.close,
            self.people_truth.close,
            self.bank.close,
            self.provisioner.close,
        ):
            closer()
        close_signer = getattr(self.signer, "close", None)
        if callable(close_signer):
            close_signer()
        if self.owns_llm:
            self.llm.close()
        self.store.close()


def build_runtime(
    *,
    world_dir: Path,
    db_path: Path,
    store: EventStore,
    clock: SimClock,
    out_dir: Path,
    world_events: Sequence[Event],
    llm: Any | None = None,
    signer: Signer | None = None,
    agents: Sequence[str] = ("invoice", "payment", "vendor"),
    score: bool = True,
    auto_controller: bool = False,
    cache_db: Path | None = None,
    beancount_path: Path | None = None,
) -> Runtime:
    world_dir = Path(world_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    def now() -> datetime:
        return clock.sim_now

    fx = load_fx_rates(world_dir / "fx_rates.json")
    ledger = Ledger(
        db_path,
        events=store,
        fx=fx,
        now=now,
        beancount_path=beancount_path or out_dir / "ledger" / "payguard.beancount",
    )
    vendors = VendorMaster(db_path)
    packets = PacketStore(db_path, events=store, now=now)
    jumper = JumperFlag(False)
    policy = Policy(ledger=ledger, vendors=vendors, events=store, jumper_present=jumper, now=now)
    invoice_state = AgentState(db_path)
    payment_state = PaymentState(db_path)
    vendor_state = VendorState(db_path)
    people_truth = PeopleTruth(db_path)
    suppliers = [
        Supplier.model_validate(s)
        for s in json.loads((world_dir / "suppliers.json").read_text(encoding="utf-8"))
    ]
    people = [
        Person.model_validate(p)
        for p in json.loads((world_dir / "people.json").read_text(encoding="utf-8"))
    ]
    owns_llm = llm is None
    # one extraction cache per world, shared with the API-backed tests: a document is paid for once
    client = (
        llm
        if llm is not None
        else LLMClient(cache_db or (world_dir.parent / "extraction_cache.db"), now=now)
    )
    chip: Signer = signer if signer is not None else StubSigner(db_path, jumper_present=jumper)
    key = chip_key()
    bank = MockBank(
        db_path, store=store, key=key, fx=fx, now=now, schedule=clock.schedule, out_dir=out_dir
    )
    provisioner = Provisioner(
        db_path,
        signer=chip,
        vendors=vendors,
        policy=policy,
        store=store,
        now=now,
        jumper_present=jumper,
        key=key,
    )
    responders = People(
        people,
        events_source=lambda: list(world_events) + clock.pending_events(),
        clock=clock,
        store=store,
        suppliers=suppliers,
        truth_store=people_truth,
        llm=client,
    )
    scoreboard = (
        scoreboard_from_world(
            world_dir, store=store, ledger=ledger, vendors=vendors, fx=fx, now=now
        )
        if score
        else None
    )
    invoice_agent = (
        InvoiceAgent(
            world_dir=world_dir,
            store=store,
            ledger=ledger,
            vendors=vendors,
            packets=packets,
            state=invoice_state,
            llm=client,
            suppliers=suppliers,
            now=now,
        )
        if "invoice" in agents
        else None
    )
    payment_agent = (
        PaymentAgent(
            store=store,
            ledger=ledger,
            vendors=vendors,
            packets=packets,
            state=payment_state,
            invoice_state=invoice_state,
            signer=chip,
            bank=bank,
            fx=fx,
            now=now,
            provisioner=provisioner,
        )
        if "payment" in agents
        else None
    )
    vendor_agent = (
        VendorAgent(
            store=store,
            ledger=ledger,
            vendors=vendors,
            packets=packets,
            state=vendor_state,
            people=responders,
            suppliers=suppliers,
            payment_state=payment_state,
            invoice_state=invoice_state,
            schedule=clock.schedule,
            now=now,
            provisioner=provisioner,
        )
        if "vendor" in agents
        else None
    )
    controller = AutoController(packets) if auto_controller else None
    active: list[Any] = [
        a for a in (invoice_agent, vendor_agent, payment_agent, controller) if a is not None
    ]
    runner = Runner(
        store=store,
        policy=policy,
        agents=active,
        now=now,
        explainer=client,
        messenger=responders,
        scoreboard=scoreboard,
    )
    runner.attach(clock)
    return Runtime(
        world_dir=world_dir,
        db_path=Path(db_path),
        out_dir=out_dir,
        store=store,
        clock=clock,
        fx=fx,
        ledger=ledger,
        vendors=vendors,
        packets=packets,
        policy=policy,
        invoice_state=invoice_state,
        payment_state=payment_state,
        vendor_state=vendor_state,
        people_truth=people_truth,
        llm=client,
        signer=chip,
        bank=bank,
        provisioner=provisioner,
        jumper=jumper,
        invoice_agent=invoice_agent,
        payment_agent=payment_agent,
        vendor_agent=vendor_agent,
        auto_controller=controller,
        runner=runner,
        scoreboard=scoreboard,
        suppliers=suppliers,
        people=people,
        responders=responders,
        owns_llm=owns_llm,
    )


def start_runtime(rt: Runtime, *, provision: bool = True) -> BootstrapResult:
    """Opening balances and vendor master (idempotent), then the pre-demo provisioning of every
    active approved account with the jumper present, after which the jumper is pulled."""
    result = bootstrap(rt.ledger, rt.store, rt.vendors, suppliers=rt.suppliers)
    if rt.payment_agent is not None and provision:
        rt.jumper.present = True
        try:
            if not rt.signer.status().key_loaded:
                rt.provisioner.load_key_and_caps()
            rt.provisioner.provision_all()
        finally:
            rt.jumper.present = False
    log.info(
        "runtime_started",
        entries_posted=result.entries_posted,
        vendors=result.vendors_seeded,
        provisioned=len(rt.provisioner.slots()),
        event_id=result.decision_event_id,
        principal="runtime",
        rule_id=None,
    )
    return result
