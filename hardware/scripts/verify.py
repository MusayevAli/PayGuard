#!/usr/bin/env python3
"""Run independent RTL and software checks; never substitutes model tests for RTL."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build" / "verification"
BUILD.mkdir(parents=True, exist_ok=True)
local_tools = ROOT.parent.parent / "tooling/iverilog/usr"
iverilog = os.environ.get("IVERILOG") or shutil.which("iverilog")
vvp = os.environ.get("VVP") or shutil.which("vvp")
base = os.environ.get("IVL_BASE")
if not iverilog and (local_tools / "bin/iverilog").is_file():
    iverilog = str(local_tools / "bin/iverilog")
    vvp = str(local_tools / "bin/vvp")
    base = str(local_tools / "lib/x86_64-linux-gnu/ivl")
results = []

def run(label, args, env=None, timeout=300):
    start = time.monotonic()
    p = subprocess.run(args, cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=timeout)
    (BUILD / (label + ".log")).write_text(p.stdout)
    results.append({"check":label,"passed":p.returncode==0,
                    "seconds":round(time.monotonic()-start,3),"log":str(Path("build/verification")/(label+".log"))})
    print(f"{'PASS' if p.returncode == 0 else 'FAIL'} {label}")
    if p.returncode:
        print(p.stdout[-10000:])
    return p.returncode == 0

env = dict(os.environ, PYTHONPATH=str(ROOT/"python"))
run("software", [sys.executable,"-m","unittest","discover","-s","tests","-v"], env=env)
if not iverilog or not vvp:
    results.append({"check":"rtl_simulator_available","passed":False})
    print("FAIL RTL simulation requires Icarus Verilog (iverilog and vvp).")
else:
    common = [str(p.relative_to(ROOT)) for p in sorted((ROOT/"rtl").glob("*.sv"))]
    for name in ("hmac","policy","core","axi","capacity","scan","clock"):
        bench = ROOT/"sim"/f"tb_{name}.sv"
        if not bench.exists():
            results.append({"check":name,"passed":False,"reason":"missing testbench"})
            continue
        args = [iverilog]
        if base:
            args += ["-B",base]
        args += ["-g2012","-Wall","-s",f"tb_{name}","-o",str(BUILD/f"tb_{name}")]
        args += common+[str(bench)]
        if run(name+"_compile",args):
            run(name+"_simulation",[vvp,str(BUILD/f"tb_{name}")])
    env.update(IVERILOG=iverilog, VVP=vvp)
    if base: env["IVL_BASE"] = base
    run("differential",[sys.executable,"sim/test_vectors.py"],env=env)

report = {"scope":"Local software and RTL simulation. Not synthesis or physical-board validation.",
          "passed":all(r["passed"] for r in results),"checks":results}
(BUILD/"results.json").write_text(json.dumps(report,indent=2)+"\n")
sys.exit(0 if report["passed"] else 1)
