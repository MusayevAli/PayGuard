"""Deterministic adversarial FPGA-policy scenario, simulated transport explicitly labelled."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from .protocol import *
from .model import PolicyModel
from .gateway import Gateway, Journal, GatewayError
from .bridge import JournaledDevice, RecoveryCoordinator
from .transport import ModelTransport

# Public, deliberately non-production vectors. Never use these in a deployed bitstream.
DEMO_KEYS={name:hashlib.sha256(("PUBLIC SILICONSIGN SIMULATION ONLY "+name).encode()).digest()
           for name in ("admin","controller","response")}

def run_demo(state_dir):
    state=Path(state_dir);state.mkdir(parents=True,exist_ok=True)
    if (state/"journal.sqlite").exists():raise ValueError("demo needs an empty directory; existing journals are never cleared")
    k=DEMO_KEYS
    model=PolicyModel(k["admin"],k["controller"],k["response"],clock=lambda:0)
    bank=Gateway(state/"bank.sqlite",k["response"]);journal=Journal(state/"journal.sqlite",k["response"])
    device=JournaledDevice(ModelTransport(model),journal,bank)
    controller=RecoveryCoordinator(device,k["admin"],k["controller"])
    controller.start_new(timestamp=1_700_006_400,daily_cap=300_000,threshold=50_000,cooldown=60,max_count=20)
    acct=digest("approved-city-supplier-account");newacct=digest("independently-verified-replacement-account")
    controller.manage(command(Op.ADD_VENDOR,vendor=0,vendor_version=1,account=acct,
                              vendor_cap=100_000,evidence=digest("SIMULATED approved vendor packet")))
    events=[];payseq=0
    def obligation(slot,amount,flags=0):
        f=command(Op.COMMIT_INVOICE,vendor=0,invoice=slot,vendor_version=model.vendors[0].version,
                  account=model.vendors[0].account,amount=amount,invoice_hash=digest(f"city-invoice-{slot}"),
                  evidence=digest(f"SIMULATED invoice/PO/receipt {slot}"),flags=flags)
        return controller.manage(f)[0]
    def attempt(name,f,expected):
        nonlocal payseq
        payseq+=1
        f=f.replace(op=Op.PAY,seq=payseq,session=controller.session)
        response,mac,receipt=device.pay(f)
        actual=Frame.unpack(response).status
        item={"scenario":name,"expected":expected.name,"actual":actual.name,
              "pass":actual==expected,"amount_cents":f.field("amount"),"receipt":receipt,
              "command":f.pack().hex(),"response":response.hex(),"response_mac":mac.hex()}
        events.append(item)
        if actual!=expected:raise AssertionError(f"{name}: {actual.name} != {expected.name}")
        return f,response,mac
    normal=obligation(0,10_000)
    ordinary=attempt("legitimate invoice settles",normal,Status.OK)
    inv=obligation(1,20_000)
    attempt("agent obeys fraudulent bank-change email",inv.replace(account=digest("attacker-account")),Status.ACCOUNT)
    attempt("raw request bypassing the AI",inv.replace(amount=25_000),Status.AMOUNT)
    attempt("previous invoice paid again",normal,Status.PAID)
    large=obligation(2,60_000,flags=1)
    attempt("large unusual payment waits",large,Status.APPROVAL)
    controller.manage(large.replace(op=Op.APPROVE_EXCEPTION,ttl=30))
    attempt("exact human-approved large payment",large,Status.OK)
    capped=obligation(3,40_000)
    controller.manage(capped.replace(op=Op.APPROVE_EXCEPTION,ttl=30))
    attempt("approval cannot waive vendor daily cap",capped,Status.VENDOR_CAP)
    # Existing unpaid invoice is safely rebound, never given a new invoice identity.
    controller.manage(command(Op.CHANGE_BANK,vendor=0,vendor_version=2,account=newacct,
                              evidence=digest("SIMULATED independent bank ownership verification")))
    rebound=controller.manage(inv.replace(op=Op.REBIND_INVOICE,vendor_version=2,account=newacct,
                                         evidence=digest("SIMULATED rebind evidence")))[0]
    controller.manage(rebound.replace(op=Op.APPROVE_EXCEPTION,ttl=120))
    attempt("verified account change still waits",rebound,Status.COOLDOWN)
    model.advance(61)  # Explicit simulation clock advance, not a hardware time-setting command.
    attempt("legitimate rebound invoice after waiting",rebound,Status.OK)
    expired=obligation(4,5_000)
    controller.manage(expired.replace(op=Op.APPROVE_EXCEPTION,ttl=1));model.advance(2)
    attempt("expired exception approval",expired,Status.APPROVAL)
    forged=bytearray(ordinary[1]);forged[48]^=1
    try:bank.settle(ordinary[0].pack(),bytes(forged),ordinary[2])
    except GatewayError:events.append({"scenario":"forged hardware response", "expected":"gateway rejection","actual":"gateway rejection","pass":True})
    else:raise AssertionError("tampered response accepted")
    retry=bank.settle(ordinary[0].pack(),ordinary[1],ordinary[2])
    events.append({"scenario":"retry identical bank authorization","expected":"same receipt","actual":"same receipt" if retry["idempotent"] else "duplicate debit","pass":retry["idempotent"]})
    # Fail-closed boot and conservative recovery demonstrate durable reservations.
    old_session=controller.session;model.reset();device.locked=True
    controller.recover(timestamp=1_700_006_463)
    attempt("consumed invoice after recovery remains unavailable",normal.replace(session=controller.session,account=newacct,vendor_version=2),Status.INVOICE)
    # No consumed invoice slot was re-provisioned; durable bank also deduplicates identity.
    balances=bank.snapshot()
    totals=balances["totals"]
    balanced=all(t["debit"]==t["credit"] for t in totals)
    if not balanced:raise AssertionError("unbalanced ledger")
    result={"mode":"SOFTWARE REFERENCE SIMULATION — NO FPGA OR REAL MONEY", "cases":events,
            "passed":sum(e["pass"] for e in events),"total":len(events),
            "settled_payment_count":len(balances["receipts"]),"books_balanced":balanced,
            "reserved_cents_after_recovery":model.spent,
            "old_session_revoked":old_session!=bank.active_session,
            "limitations":["Synthetic source documents and provider evidence", "Human approval is authenticated software, not a physical button", "Bank-change wait accelerated only by simulation clock"]}
    (state/"demo-report.json").write_text(json.dumps(result,indent=2)+"\n")
    return result
