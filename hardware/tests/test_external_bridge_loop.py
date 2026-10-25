"""UART JSON noise is inert; invalid envelopes and execution errors stop the loop.

The real journal and bridge loop run against an explicit policy-model transport.
These tests do not open a serial port or establish a physical-board result.
"""

import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from siliconsign.external_bridge import external_bridge_main
from siliconsign.gateway import Journal
from siliconsign.model import PolicyModel
from siliconsign.protocol import HELLO_REQUEST, Frame, Op, command, envelope
from siliconsign.transport import ModelTransport

ADMIN_KEY, CONTROLLER_KEY, RESPONSE_KEY = b"a" * 32, b"c" * 32, b"r" * 32
HELLO_LINE = (json.dumps(HELLO_REQUEST) + "\n").encode()


class RecordedModelDevice(ModelTransport):
    def __init__(self, fail_after_startup=False):
        super().__init__(PolicyModel(ADMIN_KEY, CONTROLLER_KEY, RESPONSE_KEY, nv=64, ni=256))
        self.operations = []
        self.fail_after_startup = fail_after_startup

    def capabilities(self):
        return {"vendors": 64, "invoices": 256}

    def clock_divider_hz(self):
        return 50_000_000

    def exchange(self, frame, mac):
        self.operations.append(Frame.unpack(frame).op)
        if self.fail_after_startup and len(self.operations) > 1:
            raise OSError("test MMIO exchange failure")
        return super().exchange(frame, mac)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


class ExternalBridgeLoopTests(unittest.TestCase):
    def run_input(self, data, failure=None):
        class FailingJournal(Journal):
            def begin(self, frame, mac):
                if self.records():
                    raise OSError("test journal write failure")
                return super().begin(frame, mac)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = root / "response.json"
            key.write_text(json.dumps({"response": RESPONSE_KEY.hex()}))
            key.chmod(0o600)
            args = SimpleNamespace(
                response_key=key,
                state_dir=root / "state",
                mmio_device="/dev/test-model-only",
                base=0,
                timeout=1,
            )
            device = RecordedModelDevice(fail_after_startup=failure == "mmio")
            output, errors = io.BytesIO(), io.StringIO()
            with (
                patch("siliconsign.transport.MMIOTransport", return_value=device),
                patch(
                    "siliconsign.external_bridge.Journal",
                    FailingJournal if failure == "journal" else Journal,
                ),
                patch("sys.stdin", SimpleNamespace(buffer=io.BytesIO(data))),
                patch("sys.stdout", SimpleNamespace(buffer=output)),
                patch("sys.stderr", errors),
            ):
                result = external_bridge_main(args)
            records = Journal(root / "state" / "external-journal.sqlite", RESPONSE_KEY).records()
            # Startup performs one authenticated-response STATUS read. Input
            # whitespace/discovery must not add initialization or payment authority.
            expected = [Op.STATUS, Op.STATUS] if failure == "mmio" else [Op.STATUS]
            self.assertEqual(device.operations, expected)
            self.assertEqual([Frame.unpack(row["command"]).op for row in records], expected)
            if failure == "mmio":
                self.assertIsNone(records[-1]["response"])
            self.assertFalse(device.model.initialized)
            self.assertEqual(device.model.auth_counter, 0)
            self.assertEqual(device.model.spent, 0)
            return result, output.getvalue(), errors.getvalue()

    def test_handoff_whitespace_before_hello_returns_informational_manifest(self):
        for blank in (b"\n", b"\r\n", b" \t\r\n", b"\v\f\t \n", b" " * 2047 + b"\n"):
            with self.subTest(blank_length=len(blank)):
                result, output, errors = self.run_input(blank + HELLO_LINE + b"\n")
                self.assertEqual(result, 0)
                lines = output.splitlines()
                self.assertEqual(len(lines), 1)
                manifest = json.loads(lines[0])
                self.assertEqual(manifest["event"], "siliconsign.info.v1")
                self.assertEqual(manifest["mode"], "external-gateway")
                self.assertEqual(manifest["capabilities"], {"vendors": 64, "invoices": 256})
                self.assertTrue(manifest["informational"])
                self.assertFalse(manifest["authorization"])
                self.assertIsNone(manifest["session"])
                self.assertNotIn("BRIDGE STOPPED", errors)

    def test_invalid_json_is_logged_without_stdout_then_hello_still_works(self):
        for junk in (b"[\n", b"not-json\n", b"\x00\n", b"\xff\n", b"\xc3\x28\n"):
            with self.subTest(junk=junk):
                result, output, errors = self.run_input(junk + HELLO_LINE)
                self.assertEqual(result, 0)
                lines = output.splitlines()
                self.assertEqual(len(lines), 1)
                manifest = json.loads(lines[0])
                self.assertEqual(manifest["event"], "siliconsign.info.v1")
                self.assertTrue(manifest["informational"])
                self.assertFalse(manifest["authorization"])
                self.assertIsNone(manifest["session"])
                self.assertIn("Rejected malformed UART JSON", errors)
                self.assertNotIn("BRIDGE STOPPED", errors)

    def test_valid_json_with_invalid_envelope_still_stops_before_following_hello(self):
        for invalid in (
            b"{}\n",
            b"[]\n",
            b"null\n",
            b' {"hello":"siliconsign.v1","extra":true}\n',
            b'{"frame":"00","mac":"00"}\n',
        ):
            with self.subTest(invalid=invalid):
                result, output, errors = self.run_input(b"\n" + invalid + HELLO_LINE)
                self.assertEqual(result, 2)
                self.assertEqual(output, b"")
                self.assertIn("BRIDGE STOPPED", errors)
                self.assertNotIn("Rejected malformed UART JSON", errors)

    def test_mmio_and_journal_errors_still_stop_after_a_valid_envelope(self):
        for failure in ("mmio", "journal"):
            with self.subTest(failure=failure):
                result, output, errors = self.run_input(
                    envelope(command(Op.STATUS)) + HELLO_LINE, failure=failure
                )
                self.assertEqual(result, 2)
                self.assertEqual(output, b"")
                self.assertIn("BRIDGE STOPPED", errors)
                self.assertNotIn("Rejected malformed UART JSON", errors)

    def test_oversized_whitespace_is_not_discarded_or_truncated_into_a_hello(self):
        for oversized in (b" " * 2048 + b"\n", b"\t" * 4096 + b"\n"):
            with self.subTest(length=len(oversized)):
                result, output, errors = self.run_input(oversized + HELLO_LINE)
                self.assertEqual(result, 2)
                self.assertEqual(output, b"")
                self.assertIn("BRIDGE STOPPED: oversized bridge request", errors)


if __name__ == "__main__":
    unittest.main()
