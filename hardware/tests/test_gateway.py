"""Adversarial settlement, accounting and journal tests; no bank/network calls."""
import concurrent.futures
import hashlib
import hmac
import os
import sqlite3
import struct
import tempfile
import unittest

from siliconsign.gateway import Gateway, GatewayError, Journal, RecoveryRequired

KEY = hashlib.sha256(b"gateway-test-key-not-for-production").digest()
SESSION = bytes.fromhex("00112233445566778899aabbccddeeff")


def frame(op=4, session=SESSION, invoice=b"invoice1", amount=85000, sequence=1):
    words = [0] * 64
    words[:4] = [0x53494c43, 1, op, sequence]
    words[4:8] = struct.unpack("<4I", session)
    words[8:16] = [0, 0, 840, 0, amount & 0xffffffff, amount >> 32, 1, 60]
    words[16:24] = struct.unpack("<8I", hashlib.sha256(b"approved-account").digest())
    words[24:32] = struct.unpack("<8I", hashlib.sha256(invoice).digest())
    words[46:54] = struct.unpack("<8I", hashlib.sha256(b"real-evidence-hash").digest())
    return struct.pack("<64I", *words)


def response(command, counter=1, status=0):
    words = list(struct.unpack("<64I", command))
    words[0] = 0x53494c52
    words[56] = status
    words[57] = counter
    words[58] = 1760000000
    words[60] = words[12]
    words[62] = 1
    words[63] = 1
    packet = struct.pack("<64I", *words)
    return packet, hmac.digest(KEY, packet, "sha256")


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "bank.sqlite3")
        self.bank = Gateway(self.path, KEY)
        self.bank.activate_session(SESSION)
        self.commit = frame()
        self.pay = frame(6)
        self.bank.recognize(self.commit)

    def tearDown(self):
        self.tmp.cleanup()

    def test_settlement_and_double_entry(self):
        packet, mac = response(self.pay)
        receipt = self.bank.settle(self.pay, packet, mac)
        self.assertFalse(receipt["idempotent"])
        self.assertEqual(receipt["amount_cents"], 85000)
        snapshot = self.bank.snapshot()
        self.assertEqual(len(self.bank.entries()), 4)
        self.assertEqual(snapshot["totals"], [{"currency": 840, "debit": 170000, "credit": 170000}])
        balances = {row["account"]: row["net"] for row in snapshot["balances"]}
        self.assertEqual(balances, {"accounts_payable": 0, "cash": -85000, "expense": 85000})

    def test_identical_retry_returns_existing_receipt(self):
        packet, mac = response(self.pay)
        first = self.bank.settle(self.pay, packet, mac)
        second = self.bank.settle(self.pay, packet, mac)
        self.assertEqual(first["receipt_id"], second["receipt_id"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(len(self.bank.entries()), 4)

    def test_concurrent_retry_exactly_once(self):
        packet, mac = response(self.pay)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            receipts = list(pool.map(lambda _: self.bank.settle(self.pay, packet, mac), range(16)))
        self.assertEqual(len({r["receipt_id"] for r in receipts}), 1)
        self.assertEqual(sum(not r["idempotent"] for r in receipts), 1)
        self.assertEqual(len(self.bank.entries()), 4)

    def test_invalid_mac(self):
        packet, _ = response(self.pay)
        with self.assertRaisesRegex(GatewayError, "authentication"):
            self.bank.settle(self.pay, packet, bytes(32))
        self.assertEqual(len(self.bank.entries()), 2)

    def test_authentic_response_cannot_authorize_changed_request(self):
        packet, mac = response(self.pay)
        changed = frame(6, amount=85001)
        with self.assertRaisesRegex(GatewayError, "submitted request"):
            self.bank.settle(changed, packet, mac)

    def test_hardware_success_without_recognized_obligation_fails(self):
        unknown = frame(6, invoice=b"invented")
        packet, mac = response(unknown)
        with self.assertRaisesRegex(GatewayError, "independently recognized"):
            self.bank.settle(unknown, packet, mac)

    def test_signed_rejection_never_pays(self):
        packet, mac = response(self.pay, status=9)
        with self.assertRaisesRegex(GatewayError, "successful payment"):
            self.bank.settle(self.pay, packet, mac)

    def test_counter_reuse_other_invoice_rejected(self):
        self.bank.settle(self.pay, *response(self.pay))
        newcommit = frame(4, invoice=b"invoice2")
        newpay = frame(6, invoice=b"invoice2")
        self.bank.recognize(newcommit)
        with self.assertRaisesRegex(GatewayError, "counter reused"):
            self.bank.settle(newpay, *response(newpay))

    def test_new_counter_cannot_pay_same_invoice_again(self):
        self.bank.settle(self.pay, *response(self.pay))
        newpay = frame(6, sequence=2)
        with self.assertRaisesRegex(GatewayError, "already settled"):
            self.bank.settle(newpay, *response(newpay, counter=2))

    def test_cross_session_double_payment_prevented(self):
        self.bank.settle(self.pay, *response(self.pay))
        second_session = b"2" * 16
        self.bank.activate_session(second_session)
        newpay = frame(6, session=second_session)
        with self.assertRaisesRegex(GatewayError, "already settled"):
            self.bank.settle(newpay, *response(newpay))
        with self.assertRaisesRegex(GatewayError, "inactive session"):
            self.bank.settle(self.pay, *response(self.pay))
        with self.assertRaisesRegex(GatewayError, "revoked"):
            self.bank.activate_session(SESSION)

    def test_recognition_cannot_rebind_invoice(self):
        self.assertTrue(self.bank.recognize(self.commit)["idempotent"])
        with self.assertRaisesRegex(GatewayError, "different payment"):
            self.bank.recognize(frame(amount=85001))

    def test_restart_preserves_payment(self):
        self.bank.settle(self.pay, *response(self.pay))
        reopened = Gateway(self.path, KEY)
        self.assertTrue(reopened.settle(self.pay, *response(self.pay))["idempotent"])
        self.assertEqual(len(reopened.entries()), 4)

    def test_storage_overflow_rejected(self):
        with self.assertRaisesRegex(GatewayError, "storage range"):
            self.bank.recognize(frame(invoice=b"huge", amount=1 << 63))

    def _rebind(self, **changes):
        words = list(struct.unpack("<64I", self.commit))
        words[2] = 11
        words[14] = 2
        words[16:24] = struct.unpack("<8I", hashlib.sha256(b"new-approved-account").digest())
        for index, value in changes.items():
            words[int(index)] = value
        return struct.pack("<64I", *words)

    def test_rebind_unpaid_preserves_accounting(self):
        rebound = self._rebind()
        result = self.bank.recognize(rebound)
        self.assertTrue(result["rebound"])
        self.assertEqual(len(self.bank.entries()), 2)
        words = list(struct.unpack("<64I", rebound))
        words[2] = 6
        payment = struct.pack("<64I", *words)
        self.bank.settle(payment, *response(payment))
        self.assertEqual(len(self.bank.entries()), 4)
        self.assertEqual(self.bank.snapshot()["totals"][0]["debit"], 170000)

    def test_rebind_settled_rejected(self):
        self.bank.settle(self.pay, *response(self.pay))
        with self.assertRaisesRegex(GatewayError, "settled"):
            self.bank.recognize(self._rebind())

    def test_rebind_issued_unsettled_rejected(self):
        with self.assertRaisesRegex(GatewayError, "issued hardware"):
            self.bank.recognize(self._rebind(), authorization_issued=True)
        self.assertEqual(len(self.bank.entries()), 2)

    def test_rebind_amount_currency_flags_vendor_and_invoice_immutable(self):
        for index, value in [(8, 1), (9, 1), (10, 978), (11, 1), (12, 85001)]:
            with self.subTest(field=index):
                with self.assertRaisesRegex(GatewayError, "cannot change"):
                    self.bank.recognize(self._rebind(**{str(index): value}))
        with self.assertRaisesRegex(GatewayError, "existing independently"):
            self.bank.recognize(self._rebind(**{"24": 1}))

    def test_rebind_requires_newer_version_and_allows_returning_to_prior_account(self):
        with self.assertRaisesRegex(GatewayError, "newer vendor version"):
            self.bank.recognize(self._rebind(**{"14": 1}))
        same = list(struct.unpack("<64I", self.commit))
        same[2] = 11
        same[14] = 3  # Bank returned to previous account after an intervening change.
        result = self.bank.recognize(struct.pack("<64I", *same))
        self.assertTrue(result["rebound"])
        self.assertEqual(len(self.bank.entries()), 2)

    def test_old_account_authorization_rejected_after_rebind(self):
        self.bank.recognize(self._rebind())
        with self.assertRaisesRegex(GatewayError, "independently recognized"):
            self.bank.settle(self.pay, *response(self.pay))

    def test_rejection_is_audited(self):
        with self.assertRaises(GatewayError):
            self.bank.settle(self.pay, *response(self.pay, status=13))
        with sqlite3.connect(self.path) as db:
            count = db.execute("SELECT COUNT(*) FROM audit WHERE kind='payment_rejected'").fetchone()[0]
        self.assertEqual(count, 1)


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "journal.sqlite3")
        self.journal = Journal(self.path, KEY)
        self.pay = frame(6)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pending_intent_survives_restart_and_blocks_recovery(self):
        intent = self.journal.begin(self.pay, bytes(32))
        reopened = Journal(self.path, KEY)
        self.assertEqual(reopened.pending()[0]["id"], intent)
        with self.assertRaises(RecoveryRequired):
            reopened.assert_recoverable()

    def test_signed_completion_is_durable_and_idempotent(self):
        intent = self.journal.begin(self.pay, bytes(32))
        packet, mac = response(self.pay)
        self.journal.finish(intent, packet, mac)
        self.journal.finish(intent, packet, mac)
        reopened = Journal(self.path, KEY)
        reopened.assert_recoverable()
        self.assertEqual(reopened.records()[0]["response"], packet)
        with self.assertRaises(GatewayError):
            reopened.finish(intent, *response(self.pay, counter=2))

    def test_invalid_mac_cannot_resolve_intent(self):
        intent = self.journal.begin(self.pay, bytes(32))
        with self.assertRaises(GatewayError):
            self.journal.finish(intent, response(self.pay)[0], bytes(32))
        self.assertEqual(len(self.journal.pending()), 1)

    def test_recovery_generation_survives_restart_and_blocks_partial_snapshot(self):
        new_session = b"N" * 16
        self.journal.begin_recovery(SESSION, new_session, {"vendors": 1})
        reopened = Journal(self.path, KEY)
        with self.assertRaisesRegex(RecoveryRequired, "incomplete recovery"):
            reopened.assert_recovery_complete()
        with self.assertRaisesRegex(RecoveryRequired, "incomplete recovery"):
            reopened.begin_recovery(new_session, b"Z" * 16, {})
        reopened.checkpoint(SESSION, new_session, {"vendors": 1})
        Journal(self.path, KEY).assert_recovery_complete()

    def test_recovery_completion_cannot_complete_wrong_generation(self):
        self.journal.begin_recovery(SESSION, b"N" * 16, {})
        with self.assertRaisesRegex(RecoveryRequired, "does not match"):
            self.journal.checkpoint(SESSION, b"Z" * 16, {})
        with self.assertRaises(RecoveryRequired):
            self.journal.assert_recovery_complete()

    def test_recovery_completion_refuses_unresolved_hardware_intent(self):
        self.journal.begin_recovery(SESSION, b"N" * 16, {})
        self.journal.begin(self.pay, bytes(32))
        with self.assertRaisesRegex(RecoveryRequired, "unresolved hardware"):
            self.journal.checkpoint(SESSION, b"N" * 16, {})

    def test_legacy_recovery_marker_is_backward_compatible(self):
        self.journal.assert_recovery_complete()
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO recovery_events(data,created_ns) VALUES(?,?)",
                       ('{"old_session":null,"new_session":"legacy","summary":{}}', 1))
        self.journal.assert_recovery_complete()
        self.journal.begin_recovery(SESSION, b"N" * 16, {})
        self.journal.checkpoint(SESSION, b"N" * 16, {})
        self.journal.assert_recovery_complete()

    def test_tampered_persistent_response_detected(self):
        intent = self.journal.begin(self.pay, bytes(32))
        self.journal.finish(intent, *response(self.pay))
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE command_journal SET response_mac=? WHERE id=?", (bytes(32), intent))
        with self.assertRaisesRegex(GatewayError, "authentication"):
            self.journal.records()


if __name__ == "__main__":
    unittest.main()
