"""Journaled FPGA transport only. PayGuard owns the separate simulated bank.

No private command keys, implicit initialization, reset, approval or settlement.
A lost/ambiguous command locks this bridge until operator reconciliation.
"""
from __future__ import annotations

import hmac
import json
import sys
from pathlib import Path

from .gateway import Journal, RecoveryRequired
from .protocol import ADMIN_OPS, CONTROLLER_OPS, HELLO_REQUEST, Frame, Op, Status, command, envelope, parse_envelope


class ExternalBridge:
    def __init__(self, transport, journal):
        self.transport, self.journal = transport, journal
        self.locked = False
        self.capabilities = transport.capabilities()
        self.clock_divider_hz = transport.clock_divider_hz()
        if self.clock_divider_hz != 50_000_000:
            raise RecoveryRequired("This bridge requires the compiled 50 MHz real-time divider")
        if self.capabilities != {"vendors": 64, "invoices": 256}:
            raise RecoveryRequired("This application requires the 64-vendor/256-obligation image")
        journal.assert_recoverable()
        rows = journal.records()
        # A crash after durable result but before ACK can be reconciled by an
        # exact signed-result match; a pending intent is never automatically replayed.
        if hasattr(transport, "_read") and transport._read(transport.STATUS) & 2:
            response, mac = transport.pending_response()
            if not rows or not rows[-1]["response"] or not hmac.compare_digest(response, rows[-1]["response"]) or not hmac.compare_digest(mac, rows[-1]["response_mac"]):
                raise RecoveryRequired("Unmatched hardware DONE; operator reconciliation required")
            transport.acknowledge()
        session, _, was_locked = self.history()
        previous_time = max((Frame.unpack(r["response"]).integer(58, 2) for r in rows if r["response"]), default=0)
        response, _ = self.exchange(command(Op.STATUS, session=session or bytes(16)).pack(), bytes(32))
        status = Frame.unpack(response)
        if was_locked or bool(session) != bool(status.words[63]) or status.integer(58, 2) < previous_time:
            self.locked = True
            raise RecoveryRequired("Hardware reset/session differs from durable journal; do not reinitialize")

    def history(self):
        session = None
        sequences = {"admin": 0, "controller": 0, "payment": 0}
        locked = False
        for row in self.journal.records():
            if not row["response"]:
                continue
            frame, response = Frame.unpack(row["command"]), Frame.unpack(row["response"])
            if response.status != Status.OK or frame.op == Op.STATUS:
                continue
            if frame.op == Op.INIT:
                if session is not None:
                    raise RecoveryRequired("Multiple initialization generations require explicit reconciliation")
                session = frame.field("session")
            if frame.op == Op.LOCK:
                locked = True
            role = "admin" if frame.op in ADMIN_OPS else "controller" if frame.op in CONTROLLER_OPS else "payment"
            sequences[role] = max(sequences[role], frame.words[3])
        return session, sequences, locked

    def info(self):
        session, sequences, locked = self.history()
        return {"event": "siliconsign.info.v1", "informational": True, "authorization": False,
                "mode": "external-gateway", "session": session.hex() if session else None,
                "locked": self.locked or locked, "capabilities": self.capabilities,
                "clock_divider_hz": self.clock_divider_hz,
                **{"next_" + k + "_sequence": v + 1 if v < 0xffffffff else None for k, v in sequences.items()}}

    def exchange(self, frame, mac):
        if self.locked:
            raise RecoveryRequired("Bridge locked; operator reconciliation required")
        intent = self.journal.begin(frame, mac)
        try:
            response, signature = self.transport.exchange(frame, mac)
            self.journal.finish(intent, response, signature)
            self.transport.acknowledge()
            return response, signature
        except BaseException:
            self.locked = True
            raise

    def handle(self, line):
        if len(line) > 2048:
            raise ValueError("oversized bridge request")
        if json.loads(line) == HELLO_REQUEST:
            return (json.dumps(self.info(), separators=(",", ":")) + "\n").encode()
        return envelope(*self.exchange(*parse_envelope(line)))


def external_bridge_main(args):
    from .__main__ import read_role_key
    from .transport import MMIOTransport
    key = read_role_key(args.response_key, "response")
    state = Path(args.state_dir)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with MMIOTransport(args.mmio_device, args.base, args.timeout) as transport:
        bridge = ExternalBridge(transport, Journal(state / "external-journal.sqlite", key))
        print(json.dumps(bridge.info()), file=sys.stderr, flush=True)
        while True:
            line = sys.stdin.buffer.readline(2049)
            if not line:
                return 0
            # Terminal handoff can leave a blank line; it grants no authority.
            # Oversized input must still reach the existing fail-closed handler.
            if len(line) <= 2048 and not line.strip():
                continue
            try:
                if len(line) <= 2048:
                    try:
                        json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        print("Rejected malformed UART JSON", file=sys.stderr, flush=True)
                        continue
                sys.stdout.buffer.write(bridge.handle(line))
                sys.stdout.buffer.flush()
            except Exception as exc:
                print("BRIDGE STOPPED: " + str(exc), file=sys.stderr, flush=True)
                return 2
