"""World generator (spec §2.10): ``python -m sim.generate_world --seed 42 --out sim/world``.

Writes suppliers.json, pos.json, receipts.json, invoices/ (documents), emails/, statements/,
people.json, fx_rates.json, answer_key.json, invoices_truth.json and world.jsonl. Everything
derives from the seed; two runs with the same seed are byte-identical.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import UTC, date
from pathlib import Path
from typing import Any

import structlog

from sim.fx import DEFAULT_CACHE, build_fx_table, fetch_ecb_history, write_fx_rates
from sim.models import Invoice, WrongPoAnswer
from sim.mt940 import write_mt940
from sim.people import roster
from sim.worldgen.answer_key import build_answer_key
from sim.worldgen.bank import build_statements
from sim.worldgen.cases import CaseRegistry, plant_post_hoc, plant_scripted
from sim.worldgen.documents import render_documents
from sim.worldgen.formats import format_money
from sim.worldgen.materialize import materialize
from sim.worldgen.messages import build_messages
from sim.worldgen.plan import Plan, assign_formats, build_baseline, open_pos
from sim.worldgen.rng import stream
from sim.worldgen.suppliers import build_suppliers
from sim.worldgen.worldevents import build_events

N_SUPPLIERS = 40
N_POS = 160
N_INVOICES = 300
N_OPEN_POS = 4


def configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def dump_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def generate(seed: int, out: Path, *, fx_cache: Path = DEFAULT_CACHE) -> dict[str, Any]:
    log = structlog.get_logger("generate_world")
    started = time.perf_counter()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / "invoices").mkdir()
    (out / "emails").mkdir()
    (out / "statements").mkdir()

    suppliers = build_suppliers(seed)
    by_id = {s.id: s for s in suppliers}
    log.info("suppliers_built", count=len(suppliers))

    history = fetch_ecb_history(cache_path=fx_cache)
    fx = build_fx_table(history)
    write_fx_rates(fx, out / "fx_rates.json")
    log.info("fx_rates_written", dates=len(fx.rates), source_window=fx.source_window.model_dump())

    registry = CaseRegistry()
    scripted_pos, standalone = plant_scripted(stream(seed, "cases"), by_id, registry)
    scripted_invoices = sum(len(sh.invoices) for po in scripted_pos for sh in po.shipments) + len(
        standalone
    )
    baseline = build_baseline(
        stream(seed, "baseline"),
        suppliers,
        n_pos=N_POS - len(scripted_pos) - N_OPEN_POS,
        n_invoices=N_INVOICES - scripted_invoices,
    )
    plant_post_hoc(stream(seed, "posthoc"), baseline, registry)
    opens = open_pos(stream(seed, "open"), suppliers, N_OPEN_POS)
    plan = Plan(pos=scripted_pos + baseline + opens, standalone=standalone)
    assign_formats(stream(seed, "formats"), plan)
    log.info(
        "plan_built",
        scripted_pos=len(scripted_pos),
        baseline_pos=len(baseline),
        open_pos=len(opens),
        standalone=len(standalone),
    )

    world = materialize(stream(seed, "materialize"), seed, suppliers, plan)
    invoices, _email_meta, doc_sizes = render_documents(stream(seed, "documents"), world, out)
    log.info("documents_rendered", invoices=len(invoices))

    # people: Jonas's wrong-PO script points at the M5 case planted for it
    people = roster()
    m5 = next(c for c in registry.cases if c.case == "M5" and c.extra.get("jonas_wrong_po"))
    m5_inv = next(i for i in invoices if i.id == m5.invoices[0].invoice_id)
    other_line = m5.pos[0].lines[1]
    jonas = next(p for p in people if p.id == "jonas.weber")
    jonas.script.wrong_po_answer = WrongPoAnswer(
        invoice_number=m5_inv.invoice_number,
        answers_from_po=m5.pos[0].po_id or "",
        answers_qty=other_line.qty,
    )
    dump_json(out / "people.json", [p.model_dump(mode="json") for p in people])

    a13 = next(c for c in registry.cases if c.case == "A13")
    statements = build_statements(
        stream(seed, "bank"), fx, a13.extra["prepayment_minor"], a13.pos[0].po_id or ""
    )
    statement_paths: dict[str, str] = {}
    for stmt in statements:
        rel = f"statements/{stmt.currency}/{stmt.statement_id}.mt940"
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_text(write_mt940(stmt), encoding="utf-8", newline="")
        statement_paths[stmt.statement_id] = rel
    log.info("statements_written", count=len(statements))

    t8 = next(c for c in registry.cases if c.case == "T8")
    big = next(i for i in invoices if i.id == t8.invoices[0].invoice_id)
    messages = build_messages(
        seed,
        by_id,
        big.invoice_number,
        format_money(big.gross_minor, big.currency, big.number_locale, big.language),
        out,
    )

    events, index = build_events(
        seed, world, invoices, messages, statements, statement_paths, doc_sizes
    )
    with (out / "world.jsonl").open("w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(
                json.dumps(
                    ev.model_dump(mode="json", exclude={"seq"}),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
    key = build_answer_key(seed, registry, invoices, index, messages)
    dump_json(out / "answer_key.json", key.model_dump(mode="json"))

    dump_json(out / "suppliers.json", [s.model_dump(mode="json") for s in suppliers])
    dump_json(out / "pos.json", [p.model_dump(mode="json") for p in world.pos])
    dump_json(out / "receipts.json", [r.model_dump(mode="json") for r in world.receipts])
    dump_json(out / "invoices_truth.json", [i.model_dump(mode="json") for i in invoices])
    summary = {
        "seed": seed,
        "suppliers": len(suppliers),
        "pos": len(world.pos),
        "receipts": len(world.receipts),
        "invoices": len(invoices),
        "formats": _format_counts(invoices),
        "events": len(events),
        "statements": len(statements),
        "headline": len(key.headline),
        "ambient": len(key.ambient),
        "seconds": round(time.perf_counter() - started, 1),
    }
    log.info("world_written", out=str(out), **summary)
    return summary


def _format_counts(invoices: list[Invoice]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for inv in invoices:
        counts[inv.format.value] = counts.get(inv.format.value, 0) + 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the PayGuard simulated world.")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=Path("sim/world"))
    parser.add_argument(
        "--fx-cache", type=Path, default=DEFAULT_CACHE, help="cached ECB eurofxref-hist.zip"
    )
    args = parser.parse_args(argv)
    configure_logging()
    generate(args.seed, args.out, fx_cache=args.fx_cache)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["UTC", "date", "generate", "main"]
