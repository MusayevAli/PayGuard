#!/usr/bin/env python3
"""Compare the executable reference to the actual authenticated RTL core.

Each scenario starts from reset. Python computes input HMACs and verifies every
RTL response HMAC independently. The reference clock is held constant and the
RTL clock divider is large enough that none of these bounded scenarios ticks a
second. Time-expiry/rollover behavior is tested separately by tb_policy.sv.

Run from anywhere: python3 sim/test_vectors.py
Uses IVERILOG, IVL_BASE and VVP overrides, system Icarus, or the task-local tool.
Generated vectors, HDL harness and detailed results go under build/differential.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from siliconsign.model import PolicyModel
from siliconsign.protocol import (ADMIN_OPS, CONTROLLER_OPS, Frame, Op, Status,
                                 authenticate, command, digest)

ADMIN = bytes(range(32))
CONTROLLER = bytes(range(32, 64))
RESPONSE = bytes(range(64, 96))
SESSION = digest("differential-session")[:16]
ACCOUNT0 = digest("vendor-0-bank")
ACCOUNT1 = digest("vendor-1-bank")
ACCOUNT2 = digest("vendor-0-new-bank")
EVIDENCE = digest("independently-approved-document-evidence")


def scenarios():
    init = command(Op.INIT, seq=1, session=SESSION, timestamp=1_000_000,
                   daily_cap=2_000_000, threshold=500_000, cooldown=3600,
                   max_count=100)
    v0 = command(Op.ADD_VENDOR, seq=2, session=SESSION, vendor=0,
                 account=ACCOUNT0, vendor_version=1, vendor_cap=1_000_000,
                 evidence=EVIDENCE)
    v1 = v0.replace(seq=3, vendor=1, account=ACCOUNT1)
    i0 = command(Op.COMMIT_INVOICE, seq=1, session=SESSION, vendor=0,
                 invoice=0, amount=1000, flags=1, account=ACCOUNT0,
                 vendor_version=1, invoice_hash=digest("invoice-0"), evidence=EVIDENCE)
    i1 = i0.replace(seq=2, vendor=1, invoice=1, amount=2000, flags=0,
                    account=ACCOUNT1, invoice_hash=digest("invoice-1"))
    base = [init, v0, v1, i0, i1]
    pay0 = i0.replace(op=Op.PAY, seq=1)
    pay1 = i1.replace(op=Op.PAY, seq=1)
    approve0 = i0.replace(op=Op.APPROVE_EXCEPTION, seq=3, ttl=30)
    change0 = command(Op.CHANGE_BANK, seq=4, session=SESSION, vendor=0,
                      account=ACCOUNT2, vendor_version=2, evidence=EVIDENCE)
    rebind0 = i0.replace(op=Op.REBIND_INVOICE, seq=3,
                         account=ACCOUNT2, vendor_version=2)
    status = command(Op.STATUS, session=SESSION)
    cases = [
        ("STATUS before provisioning", [status]),
        ("INIT currency above ISO range", [init.replace(currency=1000)]),
        ("INIT zero currency", [init.replace(currency=0)]),
        ("INIT payment-count upper bound", [init.replace(max_count=65536)]),
        ("STATUS with another session", [init, status.replace(session=digest("other")[:16])]),
        ("ordinary valid payment", base + [pay1, status]),
        ("exact exception approval and payment", base + [approve0, pay0, status]),
        ("zero approval TTL", base + [approve0.replace(ttl=0), status]),
        ("oversized approval TTL", base + [approve0.replace(ttl=301), status]),
        ("vendor differs from invoice binding", base + [pay0.replace(vendor=1, account=ACCOUNT1)]),
        ("invoice identity mismatch", base + [pay0.replace(invoice_hash=digest("unknown"))]),
        ("recipient mismatch", base + [pay0.replace(account=ACCOUNT2)]),
        ("invoice amount mismatch", base + [pay0.replace(amount=1001)]),
        ("zero payment amount", base + [pay0.replace(amount=0)]),
        ("payment currency mismatch", base + [pay0.replace(currency=978)]),
        ("payment vendor-version mismatch", base + [pay0.replace(vendor_version=2)]),
        ("payment exception flag removed", base + [pay0.replace(flags=0)]),
        ("out-of-range invoice", base + [pay0.replace(invoice=64)]),
        ("out-of-range vendor", base + [pay0.replace(vendor=16)]),
        ("wrong-currency bank change", base + [change0.replace(currency=978), status]),
        ("bank change invalidates old invoice", base + [change0, pay0]),
        ("bank change and authorized rebind", base + [change0, rebind0, status]),
        ("rebind cannot waive bank hold", base + [change0, rebind0,
            pay0.replace(account=ACCOUNT2, vendor_version=2)]),
        ("no-op rebind rejected", base + [i0.replace(op=Op.REBIND_INVOICE, seq=3)]),
        ("rebind altered amount", base + [change0, rebind0.replace(amount=1001)]),
        ("rebind missing approval evidence", base + [change0, rebind0.replace(evidence=bytes(32))]),
        ("rebind wrong invoice identity", base + [change0, rebind0.replace(invoice_hash=digest("wrong"))]),
        ("paid invoice cannot rebind", base + [pay1,
            i1.replace(op=Op.REBIND_INVOICE, seq=3)]),
        ("early day advancement", base + [command(Op.ADVANCE_DAY, seq=4,
            session=SESSION, timestamp=1_100_000)]),
        ("disabled vendor payment", base + [command(Op.DISABLE_VENDOR, seq=4,
            session=SESSION, vendor=1), pay1]),
        ("duplicate invoice identity", base + [i0.replace(seq=3, invoice=2)]),
        ("LOCK then STATUS", base + [command(Op.LOCK, seq=4, session=SESSION), status]),
        ("paid invoice reported even if recipient altered", base + [pay1,
            pay1.replace(seq=2, account=ACCOUNT2)]),
        ("spent counters survive request rejection", base + [pay1,
            pay0.replace(seq=2, amount=1001), status]),
    ]
    # Same bank hash does not make two different approved vendor identities
    # interchangeable for one invoice; this isolates the identity check.
    same_account_base = [init, v0, v1.replace(account=ACCOUNT0), i0]
    cases.append(("same-account vendor identity substitution", same_account_base +
                  [pay0.replace(vendor=1)]))
    return cases


def toolchain():
    local = ROOT.parent / "tooling/iverilog/usr"
    compiler = os.environ.get("IVERILOG") or shutil.which("iverilog")
    runtime = os.environ.get("VVP") or shutil.which("vvp")
    base = os.environ.get("IVL_BASE")
    if not compiler and (local / "bin/iverilog").is_file():
        compiler = str(local / "bin/iverilog")
        runtime = str(local / "bin/vvp")
        base = str(local / "lib/x86_64-linux-gnu/ivl")
    if not compiler or not runtime:
        raise RuntimeError("Icarus compiler and vvp are required; this check does not substitute the model for RTL")
    return [compiler] + (["-B", base] if base else []), runtime


def main():
    build = ROOT / "build/differential"
    build.mkdir(parents=True, exist_ok=True)
    vectors = []
    for scenario_name, requests in scenarios():
        model = PolicyModel(ADMIN, CONTROLLER, RESPONSE, clock=lambda: 0)
        for step, request in enumerate(requests):
            key = ADMIN if request.op in ADMIN_OPS else CONTROLLER if request.op in CONTROLLER_OPS else None
            tag = authenticate(key, request) if key else bytes(32)
            expected, _ = model.process(request, tag)
            vectors.append({"scenario":scenario_name, "step":step, "reset":int(step == 0),
                            "request":request, "tag":tag, "expected":Frame.unpack(expected)})
    count = len(vectors)
    (build / "commands.hex").write_text("".join(v["request"].pack()[::-1].hex()+"\n" for v in vectors))
    (build / "tags.hex").write_text("".join(v["tag"].hex()+"\n" for v in vectors))
    (build / "resets.hex").write_text("".join(str(v["reset"])+"\n" for v in vectors))
    bench = f'''`timescale 1ns/1ps
module tb_vectors;
 reg clk=0; always #10 clk=~clk;
 reg rst_n=0,start=0;
 reg [2047:0] command=0;
 reg [255:0] command_mac=0;
 wire busy,done; wire [2047:0] response; wire [255:0] response_mac;
 // Keep the original reference-model boundary scenarios at their explicit
 // 16/64 capacity. Production 64/256 boundaries are covered by tb_capacity.
 siliconsign_core #(.NV(16),.NI(64),.ADMIN_KEY(256'h{ADMIN.hex()}),
 .CONTROLLER_KEY(256'h{CONTROLLER.hex()}),.RESPONSE_KEY(256'h{RESPONSE.hex()})) dut
 (.clk(clk),.rst_n(rst_n),.start(start),.command(command),.command_mac(command_mac),
 .busy(busy),.done(done),.response(response),.response_mac(response_mac));
 reg [2047:0] requests[0:{count-1}];
 reg [255:0] tags[0:{count-1}];
 reg resets[0:{count-1}];
 integer i,cycles;
 initial begin
  $readmemh("commands.hex",requests); $readmemh("tags.hex",tags); $readmemh("resets.hex",resets);
  for(i=0;i<{count};i=i+1) begin
   if(resets[i]) begin
    @(negedge clk); rst_n=0;
    repeat(2) @(negedge clk); rst_n=1;
   end
   @(negedge clk); command=requests[i]; command_mac=tags[i]; start=1;
   @(negedge clk); start=0; cycles=0;
   while(!done && cycles<2000) begin @(negedge clk); cycles=cycles+1; end
   if(!done) $fatal(1,"timeout vector %0d",i);
   $display("ROW %0d %h %h",i,response,response_mac);
   @(negedge clk);
  end
  $finish;
 end
 initial begin #100000000; $fatal(1,"global vector timeout"); end
endmodule
'''
    (build / "tb_vectors.sv").write_text(bench)
    compiler, runtime = toolchain()
    args = compiler + ["-g2012", "-s", "tb_vectors", "-o", str(build/"tb_vectors")]
    args += [str(ROOT/"rtl"/name) for name in ("sha256_core.sv","hmac_sha256.sv","policy_engine.sv","siliconsign_core.sv")]
    args += [str(build/"tb_vectors.sv")]
    compiled = subprocess.run(args, capture_output=True, text=True, timeout=120)
    if compiled.returncode:
        print(compiled.stdout+compiled.stderr)
        return 1
    simulated = subprocess.run([runtime, str(build/"tb_vectors")], cwd=build,
                               capture_output=True, text=True, timeout=120)
    (build / "simulation.log").write_text(simulated.stdout+simulated.stderr)
    if simulated.returncode:
        print(simulated.stdout[-5000:]+simulated.stderr)
        return 1
    rows = [line.split() for line in simulated.stdout.splitlines() if line.startswith("ROW ")]
    if len(rows) != count:
        raise RuntimeError(f"expected {count} responses, received {len(rows)}")
    failures=[]
    for row, vector in zip(rows, vectors):
        _, index, frame_hex, tag_hex = row
        data = bytes.fromhex(frame_hex)[::-1]
        actual = Frame.unpack(data)
        correct_tag = hmac.digest(RESPONSE, data, hashlib.sha256)
        if not hmac.compare_digest(correct_tag, bytes.fromhex(tag_hex)):
            failures.append({"scenario":vector["scenario"], "step":vector["step"], "error":"invalid RTL response HMAC"})
        expected = vector["expected"]
        if actual.words != expected.words:
            failure={"scenario":vector["scenario"], "step":vector["step"],
                     "op":vector["request"].op.name,
                     "model_status":expected.status.name,"rtl_status":actual.status.name,
                     "differing_words":[i for i,(a,b) in enumerate(zip(actual.words,expected.words)) if a!=b]}
            failures.append(failure)
    report={"passed":not failures,"scenarios":len(scenarios()),"responses":count,
            "response_hmacs_checked":count,"failures":failures}
    (build/"results.json").write_text(json.dumps(report,indent=2)+"\n")
    if failures:
        for f in failures:
            print("FAIL",json.dumps(f,sort_keys=True))
        print(f"FAIL differential: {len(failures)} mismatches across {count} authenticated RTL responses")
        return 1
    print(f"PASS differential: {len(scenarios())} scenarios, {count} full response comparisons and independent HMAC checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
