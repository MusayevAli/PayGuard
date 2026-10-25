"""CLI for simulated demo, trusted provisioning, evidence and actual FPGA bridging."""
from __future__ import annotations
import argparse
import hmac
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
from .protocol import *
from .model import PolicyModel
from .transport import MMIOTransport, SerialTransport
from .gateway import Gateway, Journal
from .bridge import JournaledDevice, RecoveryCoordinator, submit_envelope
from .evidence import make_packet,canonical_bytes,vetting_missing
from .demo import run_demo

def read_keys(path):
    p=Path(path)
    if os.name=="posix" and p.stat().st_mode&0o077:
        raise ValueError("key file must be private (chmod 600)")
    obj=json.loads(p.read_text())
    keys={role:bytes.fromhex(obj[role]) for role in ("admin","controller","response")}
    if any(len(k)!=32 or not any(k) for k in keys.values()) or len(set(keys.values()))!=3:
        raise ValueError("keys must be distinct nonzero 32-byte values")
    return keys

def read_role_key(path,role):
    if role not in {"admin","controller","response"}:raise ValueError("unknown key role")
    p=Path(path)
    if os.name=="posix" and p.stat().st_mode&0o077:
        raise ValueError("key file must be private (chmod 600)")
    obj=json.loads(p.read_text())
    if not isinstance(obj,dict) or role not in obj:
        raise ValueError("key file does not contain the requested role")
    key=bytes.fromhex(obj[role])
    if len(key)!=32 or not any(key):raise ValueError("role key must be nonzero 32-byte value")
    return key

def read_request(path):return parse_envelope(Path(path).read_bytes())

def frame_from_json(obj):
    data=dict(obj);op=Op[data.pop("op")]
    for key in BYTES_FIELDS:
        if key in data:data[key]=bytes.fromhex(data[key])
    return command(op,**data)

def provision_config(coordinator,config):
    options=config.get("policy",{})
    coordinator.start_new(**options)
    for record in config.get("vendors",[]):
        if not record.get("synthetic",False):
            missing=vetting_missing(record.get("checks",{}))
            if missing:raise ValueError("vendor needs externally verified reports: "+", ".join(missing))
        frame=frame_from_json(record["command"])
        if frame.op!=Op.ADD_VENDOR:raise ValueError("vendor configuration requires ADD_VENDOR")
        coordinator.manage(frame)
    for record in config.get("invoices",[]):
        frame=frame_from_json(record["command"])
        if frame.op!=Op.COMMIT_INVOICE:raise ValueError("invoice configuration requires COMMIT_INVOICE")
        coordinator.manage(frame)

def bridge_info(coordinator):
    """Informational discovery only: no keys, no FPGA command, no state mutation."""
    device=coordinator.device
    with device.mutex:
        last_payment=0
        for row in device.journal.records():
            if row["response"] is None:continue
            frame=Frame.unpack(row["command"])
            if frame.op==Op.PAY and frame.field("session")==coordinator.session and Frame.unpack(row["response"]).status==Status.OK:
                last_payment=max(last_payment,frame.words[3])
        def next_seq(value):return value+1 if value<0xffffffff else None
        return {"event":"siliconsign.info.v1","informational":True,"authorization":False,
                "mode":"ACTUAL FPGA MMIO / SIMULATED BANK" if device.transport.is_hardware else "SOFTWARE REFERENCE MODEL / SIMULATED BANK",
                "session":coordinator.session.hex() if coordinator.session else None,"locked":bool(device.locked),
                "next_admin_sequence":next_seq(coordinator.admin_seq),
                "next_controller_sequence":next_seq(coordinator.controller_seq),
                "next_payment_sequence":next_seq(last_payment)}

def handle_bridge_line(coordinator,line):
    if len(line)>2048:raise ValueError("oversized bridge request")
    # Only this exact object is a hello. Extra fields, wrong version, and a
    # hello mixed with a payment envelope are not accepted as discovery.
    obj=json.loads(line)
    if isinstance(obj,dict) and obj==HELLO_REQUEST:
        return (json.dumps(bridge_info(coordinator),separators=(",",":"))+"\n").encode()
    data,mac=parse_envelope(line);frame=Frame.unpack(data)
    response,signature=submit_envelope(coordinator,frame,mac)
    return envelope(response,signature)

def bridge_main(args):
    keys=read_keys(args.keys);state=Path(args.state_dir);state.mkdir(parents=True,exist_ok=True)
    with MMIOTransport(args.mmio_device,args.base,args.timeout) as transport:
        bank=Gateway(state/"bank.sqlite",keys["response"])
        journal=Journal(state/"journal.sqlite",keys["response"])
        device=JournaledDevice(transport,journal,bank)
        coordinator=RecoveryCoordinator(device,keys["admin"],keys["controller"])
        # On restart old authority must be revoked before any outgoing response.
        if args.recover:
            coordinator.recover()
        elif args.config:
            provision_config(coordinator,json.loads(Path(args.config).read_text()))
        else:
            bank.revoke_active_session()
            raise ValueError("provide --config for empty state or --recover after a trusted FPGA reset")
        manifest={"mode":"ACTUAL FPGA MMIO / SIMULATED BANK","session":coordinator.session.hex(),
                  "next_admin_sequence":coordinator.admin_seq+1,"next_controller_sequence":coordinator.controller_seq+1}
        (state/"session.json").write_text(json.dumps(manifest,indent=2)+"\n")
        print(json.dumps(manifest),file=sys.stderr,flush=True)
        # Newline envelopes only. No network listener, shell execution or raw key endpoint.
        while True:
            line=sys.stdin.buffer.readline(2049)
            if not line:break
            try:
                output=handle_bridge_line(coordinator,line)
                sys.stdout.buffer.write(output);sys.stdout.buffer.flush()
            except Exception as exc:
                print("BRIDGE STOPPED: "+str(exc),file=sys.stderr,flush=True)
                # Never hide a possibly-issued request behind a fabricated response.
                device.locked=True;bank.revoke_active_session();return 2
        return 0

def main(argv=None):
    parser=argparse.ArgumentParser(description="SiliconSign controller tooling; no real-money network")
    sub=parser.add_subparsers(dest="action",required=True)
    demo=sub.add_parser("demo",help="explicit software-reference adversarial demonstration")
    demo.add_argument("--state-dir")
    keys=sub.add_parser("keygen",help="generate private non-production-development key bundle")
    keys.add_argument("--output",required=True)
    req=sub.add_parser("request",help="encode canonical command JSON, unsigned")
    req.add_argument("--input",required=True);req.add_argument("--output",required=True)
    packet=sub.add_parser("packet",help="export full review evidence and bind its digest")
    packet.add_argument("--request",required=True);packet.add_argument("--sources",required=True)
    packet.add_argument("--output",required=True);packet.add_argument("--bound-request",required=True)
    packet.add_argument("--simulated",action="store_true")
    sign=sub.add_parser("sign",help="trusted human/admin signs exact reviewed command")
    sign.add_argument("--request",required=True);sign.add_argument("--keys",required=True)
    sign.add_argument("--role",required=True,choices=["admin","controller"])
    sign.add_argument("--output",required=True)
    sign.add_argument("--confirm-digest",help="explicit pre-reviewed command SHA256; otherwise interactive confirmation")
    bridge=sub.add_parser("bridge",help="actual trusted KV260 MMIO bridge over stdin/stdout JSON lines")
    bridge.add_argument("--keys",required=True);bridge.add_argument("--state-dir",required=True)
    bridge.add_argument("--mmio-device",required=True);bridge.add_argument("--base",type=lambda s:int(s,0),default=0)
    bridge.add_argument("--timeout",type=float,default=5.0);bridge.add_argument("--config")
    bridge.add_argument("--recover",action="store_true")
    external=sub.add_parser("external-bridge",help="journaled MMIO transport for the PayGuard gateway; no local bank")
    external.add_argument("--response-key",required=True)
    external.add_argument("--state-dir",required=True)
    external.add_argument("--mmio-device",default="/dev/mem")
    external.add_argument("--base",type=lambda s:int(s,0),default=0xA0000000)
    external.add_argument("--timeout",type=float,default=5.0)
    serial=sub.add_parser("serial",help="send a canonical envelope to a configured UART bridge")
    serial.add_argument("--device",required=True);serial.add_argument("--request",required=True)
    serial_info=sub.add_parser("serial-info",help="read-only USB/UART session discovery; not payment authorization")
    serial_info.add_argument("--device",required=True)
    args=parser.parse_args(argv)
    if args.action=="demo":
        state=args.state_dir or tempfile.mkdtemp(prefix="siliconsign-demo-")
        report=run_demo(state)
        print(json.dumps({"mode":report["mode"],"passed":report["passed"],"total":report["total"],
                          "books_balanced":report["books_balanced"],"report":str(Path(state)/"demo-report.json")},indent=2));return 0
    if args.action=="keygen":
        p=Path(args.output);p.parent.mkdir(parents=True,exist_ok=True)
        values={role:secrets.token_hex(32) for role in ("admin","controller","response")}
        fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,"w") as f:json.dump(values,f,indent=2);f.write("\n")
        print("Private key bundle created; never give this file to the agent process.");return 0
    if args.action=="request":
        f=frame_from_json(json.loads(Path(args.input).read_text()))
        Path(args.output).write_bytes(envelope(f));return 0
    if args.action=="packet":
        raw,_=read_request(args.request);f=Frame.unpack(raw)
        sources=json.loads(Path(args.sources).read_text())
        packet,packet_hash=make_packet(f,sources,simulated=args.simulated)
        Path(args.output).write_text(json.dumps(packet,indent=2,ensure_ascii=False)+"\n")
        Path(args.bound_request).write_bytes(envelope(f.replace(evidence=packet_hash)))
        print("Packet saved. Provider claims still require independent verification.");return 0
    if args.action=="sign":
        raw,_=read_request(args.request);f=Frame.unpack(raw);key=read_role_key(args.keys,args.role)
        allowed=ADMIN_OPS if args.role=="admin" else CONTROLLER_OPS
        if f.op not in allowed:raise ValueError("selected role cannot sign this operation")
        fingerprint=digest(raw).hex()
        review={"action":f.op.name,"amount_cents":f.field("amount"),"currency":f.words[10],
                "recipient_hash":f.field("account").hex(),"invoice_hash":f.field("invoice_hash").hex(),
                "vendor_version":f.words[14],"session":f.field("session").hex(),"sequence":f.words[3],
                "evidence_hash":f.field("evidence").hex(),"command_sha256":fingerprint}
        print(json.dumps(review,indent=2),file=sys.stderr)
        confirmation=args.confirm_digest
        if confirmation is None:confirmation=input("After reviewing the evidence, paste the exact command_sha256 to approve: ")
        if not hmac.compare_digest(confirmation.strip(),fingerprint):raise ValueError("exact transaction was not approved")
        Path(args.output).write_bytes(envelope(f,authenticate(key,f)))
        return 0
    if args.action=="external-bridge":
        from .external_bridge import external_bridge_main
        return external_bridge_main(args)
    if args.action=="bridge":return bridge_main(args)
    if args.action=="serial-info":
        transport=SerialTransport(args.device)
        try:info=transport.info()
        finally:transport.close()
        print(json.dumps(info,indent=2));return 0
    if args.action=="serial":
        raw,mac=read_request(args.request);transport=SerialTransport(args.device)
        try:response,signature=transport.exchange(raw,mac)
        finally:transport.close()
        sys.stdout.buffer.write(envelope(response,signature));return 0
    return 1

if __name__=="__main__":
    try:raise SystemExit(main())
    except (ValueError,OSError,RuntimeError) as exc:
        print("SiliconSign: "+str(exc),file=sys.stderr);raise SystemExit(2)
