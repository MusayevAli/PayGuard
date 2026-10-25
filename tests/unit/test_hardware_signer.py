"""Hardware-backend contract tests using an explicit reference-model transport.

These verify host safety/encoding, not FPGA execution or physical UART operation.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

# isort: off
# authority establishes the vendored client import path.
from signer.authority import (
    canonical_binding,
    load_role_key,
    verify_authorization,
)
from signer.frames import PayFrame, ProvisionFrame, encode
from signer.hardware import HardwareLocked, HardwareSigner
from siliconsign.model import PolicyModel
from siliconsign.protocol import Frame, Op, authenticate
# isort: on

KEYS = (bytes([1]) * 32, bytes([2]) * 32, bytes([3]) * 32)
ACCOUNT = hashlib.sha256(b"approved account").digest()
OTHER_ACCOUNT = hashlib.sha256(b"changed account").digest()
START = 1_791_633_600  # Real-clock fixture, independent of the app's simulated date.


class ExplicitModelTransport:
    is_hardware = False

    def __init__(self):
        self.elapsed = 0
        self.model = PolicyModel(*KEYS, clock=lambda: self.elapsed, nv=64, ni=256)
        self.requests = []
        self.capabilities = {"vendors": 64, "invoices": 256}
        self.clock_divider_hz = 50_000_000
        self.bad_mac = False
        self.lose_response = False

    def info(self):
        m = self.model
        return {
            "event": "siliconsign.info.v1",
            "informational": True,
            "authorization": False,
            "mode": "external-gateway",
            "session": m.session.hex() if m.initialized else None,
            "locked": m.locked,
            "next_admin_sequence": m.admin_seq + 1,
            "next_controller_sequence": m.controller_seq + 1,
            "next_payment_sequence": m.pay_seq + 1,
            "capabilities": self.capabilities,
            "clock_divider_hz": self.clock_divider_hz,
        }

    def exchange(self, frame, mac):
        self.requests.append((Frame.unpack(frame), mac))
        result, tag = self.model.process(frame, mac)
        if self.lose_response:
            raise TimeoutError("deliberately lost model response")
        return result, bytes(32) if self.bad_mac else tag

    def close(self):
        pass


def make_signer(path: Path, transport=None, **kwargs):
    transport = transport or ExplicitModelTransport()
    signer = HardwareSigner(
        path,
        transport=transport,
        admin_key=KEYS[0],
        controller_key=KEYS[1],
        response_key=KEYS[2],
        initialize=True,
        now=lambda: START,
        **kwargs,
    )
    return signer, transport


def request(
    signer,
    pay_id="PAY-000001",
    amount=100,
    *,
    account=ACCOUNT,
    vendor="V-0001",
    nonce=0,
    currency="EUR",
    native=None,
):
    native = amount if native is None else native
    raw = encode(
        PayFrame(
            seq=signer.status().seq + 1,
            account_hash=account,
            amount_minor=amount,
            currency=currency,
            nonce=nonce,
            pay_id=pay_id,
        )
    )
    binding = {
        "pay_id": pay_id,
        "vendor_id": vendor,
        "account_hash": account.hex(),
        "amount_minor": native,
        "currency": currency,
        "eur_equiv_minor": amount,
        "settlement_currency": currency,
        "settlement_minor": native,
        "payable_ids": ["AP-" + pay_id],
        "covered": {"AP-" + pay_id: native},
        "credits": {},
        "discount_minor": 0,
        "fx_rate": "1",
        "rate_date": "2026-10-25",
    }
    signer.prepare_payment(raw, binding)
    return raw, binding


def test_full_response_proof_no_legacy_tag(tmp_path):
    signer, transport = make_signer(tmp_path / "state.db")
    assert signer.provision_vendor("V-0001", ACCOUNT, 10_000).ok
    raw, binding = request(signer, currency="USD", native=109)
    result = signer.sign(raw)
    assert result.ok and result.tag is None and len(result.raw) == 256
    response = verify_authorization(result.authorization, raw, KEYS[2], binding, signer.session)
    assert response.field("amount") == 100 and response.field("currency") == 978
    assert result.authorization["binding"]["settlement_minor"] == 109
    assert transport.requests[-1][0].op == Op.PAY
    assert transport.requests[-1][1] == bytes(32)
    commit = next(pair for pair in transport.requests if pair[0].op == Op.COMMIT_INVOICE)
    assert commit[1] == authenticate(KEYS[1], commit[0])
    assert signer.status().seq == 1


def test_all_40_vendors_fit_and_signed_capacity_report(tmp_path):
    signer, transport = make_signer(tmp_path / "state.db")
    for i in range(40):
        assert signer.provision_vendor(
            f"V-{i:04d}", hashlib.sha256(str(i).encode()).digest(), 10_000
        ).ok
    assert len(transport.model.vendors) == signer.status().slots_used == 40
    assert signer.status().hardware["capacity"] == {"vendors": 64, "invoices": 256}
    assert signer.status().hardware["clock_divider_hz"] == 50_000_000


def test_old_16_vendor_image_refused_before_init(tmp_path):
    transport = ExplicitModelTransport()
    transport.capabilities = {"vendors": 16, "invoices": 64}
    with pytest.raises(HardwareLocked, match="earlier 16-vendor"):
        make_signer(tmp_path / "state.db", transport)
    assert transport.requests == []


@pytest.mark.parametrize("divider", [100_000_000, 49_999_501, None])
def test_incompatible_or_missing_clock_divider_refused_before_commands(tmp_path, divider):
    transport = ExplicitModelTransport()
    transport.clock_divider_hz = divider
    if divider is None:
        original_info = transport.info

        def missing_divider():
            result = original_info()
            del result["clock_divider_hz"]
            return result

        transport.info = missing_divider
    with pytest.raises(HardwareLocked, match="50 MHz FPGA image"):
        make_signer(tmp_path / "state.db", transport)
    assert transport.requests == []


def test_fresh_session_requires_explicit_initialization(tmp_path):
    with pytest.raises(HardwareLocked, match="PAYGUARD_HW_INITIALIZE"):
        HardwareSigner(
            tmp_path / "state.db",
            transport=ExplicitModelTransport(),
            admin_key=KEYS[0],
            controller_key=KEYS[1],
            response_key=KEYS[2],
        )


def test_clean_restart_keeps_session_caps_paid_batch_and_sequences(tmp_path):
    path = tmp_path / "state.db"
    signer, transport = make_signer(path)
    signer.provision_vendor("V-0001", ACCOUNT, 10_000)
    raw, _ = request(signer)
    assert signer.sign(raw).ok
    session = signer.session
    signer.close()
    reopened, _ = make_signer(path, transport)
    assert reopened.session == session
    assert reopened.status().daily_spent_minor == 100
    assert reopened.status().seq == 1
    assert len([f for f, _ in transport.requests if f.op == Op.INIT]) == 1


def test_restart_after_fpga_reset_is_locked_without_reinitializing(tmp_path):
    path = tmp_path / "state.db"
    signer, transport = make_signer(path)
    signer.close()
    transport.model.reset()
    with pytest.raises(HardwareLocked, match="session changed"):
        make_signer(path, transport)
    assert not transport.model.initialized


def test_malformed_persisted_state_is_not_coerced_or_reinitialized(tmp_path):
    path = tmp_path / "state.db"
    signer, transport = make_signer(path)
    state = dict(signer._state, admin_seq="1")
    signer._con.execute("UPDATE pg_hw_state SET data=?", (json.dumps(state),))
    signer.close()
    before = len(transport.requests)
    with pytest.raises(HardwareLocked, match="state integer"):
        make_signer(path, transport)
    assert len(transport.requests) == before


def test_invalid_response_mac_locks_persistently(tmp_path):
    path = tmp_path / "state.db"
    signer, transport = make_signer(path)
    transport.bad_mac = True
    with pytest.raises(HardwareLocked, match="invalid response MAC"):
        signer.status()
    signer.close()
    transport.bad_mac = False
    with pytest.raises(HardwareLocked, match="unfinished hardware transaction"):
        make_signer(path, transport)


def test_lost_pay_response_is_not_retried_or_released(tmp_path):
    path = tmp_path / "state.db"
    signer, transport = make_signer(path)
    signer.provision_vendor("V-0001", ACCOUNT, 10_000)
    raw, _ = request(signer)
    original = transport.exchange

    def lose_only_pay(frame, mac):
        transport.lose_response = Frame.unpack(frame).op == Op.PAY
        return original(frame, mac)

    transport.exchange = lose_only_pay
    with pytest.raises(HardwareLocked, match="uncertain hardware transaction"):
        signer.sign(raw)
    assert transport.model.spent == 100 and transport.model.auth_counter == 1
    with pytest.raises(HardwareLocked):
        signer.sign(raw)
    assert len([f for f, _ in transport.requests if f.op == Op.PAY]) == 1
    signer.close()
    with pytest.raises(HardwareLocked):
        make_signer(path, transport)


def test_changed_settlement_cannot_reuse_committed_identity(tmp_path):
    signer, _ = make_signer(tmp_path / "state.db")
    raw, binding = request(signer)
    changed = {**binding, "settlement_minor": 101}
    with pytest.raises(ValueError, match="different settlement"):
        signer.prepare_payment(raw, changed)


def test_approval_is_exact_batch_not_global_nonce(tmp_path):
    signer, transport = make_signer(tmp_path / "state.db", daily_cap=10_000, threshold=100)
    signer.provision_vendor("V-0001", ACCOUNT, 10_000)
    a, _ = request(signer, amount=200)
    assert signer.sign(a).reason == "needs_button"
    b, _ = request(signer, pay_id="PAY-000002", amount=201)
    assert signer.sign(b).reason == "needs_button"
    handle = signer.approve_pending("PAY-000001")
    assert signer.approval_ready("PAY-000001")
    assert not signer.approval_ready("PAY-000002")
    other = encode(
        PayFrame(
            seq=1,
            account_hash=ACCOUNT,
            amount_minor=201,
            currency="EUR",
            nonce=handle,
            pay_id="PAY-000002",
        )
    )
    assert signer.sign(other).reason == "bad_nonce"
    assert signer.sign(b).reason == "needs_button"
    assert signer.sign(a).ok  # No global nonce required: the FPGA binds this exact batch.
    assert not signer.approval_ready("PAY-000001")
    assert transport.model.spent == 200


def test_approval_expires_on_real_clock(tmp_path):
    signer, transport = make_signer(tmp_path / "state.db", daily_cap=10_000, threshold=100)
    signer.provision_vendor("V-0001", ACCOUNT, 10_000)
    raw, _ = request(signer, amount=200)
    assert signer.sign(raw).reason == "needs_button"
    signer.approve_pending("PAY-000001")
    transport.elapsed += 300
    assert not signer.approval_ready("PAY-000001")
    assert signer.sign(raw).reason == "needs_button"


def test_bank_change_preserves_vendor_and_real_hold(tmp_path):
    signer, transport = make_signer(tmp_path / "state.db")
    signer.provision_vendor("V-0001", ACCOUNT, 10_000)
    first, _ = request(signer)
    assert signer.sign(first).ok
    assert signer.provision_vendor("V-0001", OTHER_ACCOUNT, 10_000).ok
    assert len(transport.model.vendors) == 1
    assert transport.model.vendors[0].spent == 100
    assert transport.model.vendors[0].version == 2
    raw, _ = request(signer, pay_id="PAY-000002", account=OTHER_ACCOUNT)
    assert signer.sign(raw).reason == "in_hold"
    signer.tick_day(65535)
    assert signer.sign(raw).reason == "in_hold"
    transport.elapsed += 59
    assert signer.sign(raw).reason == "in_hold"
    transport.elapsed += 1
    assert signer.sign(raw).ok
    assert transport.model.vendors[0].spent == 200


def test_fake_account_of_existing_vendor_reaches_chip_rejection(tmp_path):
    signer, transport = make_signer(tmp_path / "state.db")
    signer.provision_vendor("V-0001", ACCOUNT, 10_000)
    raw, binding = request(signer, account=OTHER_ACCOUNT)
    result = signer.sign(raw)
    assert not result.ok and result.reason == "not_allowlisted" and len(result.raw) == 256
    assert transport.requests[-1][0].op == Op.COMMIT_INVOICE
    assert transport.model.spent == 0
    proof = result.authorization
    assert proof is not None
    assert bytes.fromhex(proof["response_mac_hex"]) == authenticate(KEYS[2], result.raw)
    with pytest.raises(ValueError, match="PAY command"):
        verify_authorization(proof, raw, KEYS[2], binding, signer.session)


def test_daily_budget_rolls_only_after_hardware_day_reached(tmp_path):
    signer, transport = make_signer(tmp_path / "state.db")
    signer.provision_vendor("V-0001", ACCOUNT, 10_000)
    raw, _ = request(signer)
    assert signer.sign(raw).ok
    signer.tick_day(65535)
    assert transport.model.spent == 100 and transport.model.now == START
    assert not [f for f, _ in transport.requests if f.op == Op.ADVANCE_DAY]
    transport.elapsed += 86400
    signer.tick_day(0)
    advanced = [f for f, _ in transport.requests if f.op == Op.ADVANCE_DAY]
    assert len(advanced) == 1 and advanced[0].field("timestamp") == transport.model.now
    assert transport.model.spent == 0


def test_vendor_caps_are_cumulative_daily(tmp_path):
    signer, _ = make_signer(tmp_path / "state.db")
    signer.provision_vendor("V-0001", ACCOUNT, 150)
    a, _ = request(signer)
    assert signer.sign(a).ok
    b, _ = request(signer, pay_id="PAY-000002")
    assert signer.sign(b).reason == "over_account_cap"


def test_provisioning_requires_approved_non_email_channel(tmp_path):
    signer, transport = make_signer(tmp_path / "state.db")
    signer._con.execute(
        "CREATE TABLE vendor_bank_accounts (vendor_id TEXT,account_hash TEXT,status TEXT,approved_by TEXT,verified_via TEXT,requested_via TEXT)"
    )
    signer._con.execute(
        "INSERT INTO vendor_bank_accounts VALUES(?,?,?,?,?,?)",
        ("V-0001", ACCOUNT.hex(), "active", "human", "email", "phone"),
    )
    raw = encode(ProvisionFrame(slot=0, account_hash=ACCOUNT, account_cap=1000, hold_until_day=273))
    assert signer.sign(raw).reason == "vendor_evidence_required"
    assert not transport.model.vendors
    signer._con.execute(
        "UPDATE vendor_bank_accounts SET verified_via='verified-phone',requested_via='email'"
    )
    assert signer.sign(raw).reason == "vendor_due_diligence_unavailable"
    assert not transport.model.vendors
    # Low-level administrator test setup is separate from production enrollment.
    assert signer.provision_vendor("V-0001", ACCOUNT, 1000).ok
    signer._con.execute("UPDATE vendor_bank_accounts SET account_hash=?", (OTHER_ACCOUNT.hex(),))
    raw = encode(
        ProvisionFrame(slot=1, account_hash=OTHER_ACCOUNT, account_cap=1000, hold_until_day=273)
    )
    assert signer.sign(raw).ok
    assert len(transport.model.vendors) == 1
    assert transport.model.vendors[0].version == 2
    assert transport.model.vendors[0].hold_until == transport.model.now + 60


def test_fixture_onboarding_is_explicit_exact_and_scoped(tmp_path):
    signer, transport = make_signer(tmp_path / "state.db")
    signer._con.execute(
        "CREATE TABLE vendor_bank_accounts (vendor_id TEXT,account_hash TEXT,status TEXT,approved_by TEXT,verified_via TEXT,requested_via TEXT,approval_event_id TEXT)"
    )
    signer._con.execute(
        "INSERT INTO vendor_bank_accounts VALUES(?,?,?,?,?,?,?)",
        ("V-0001", ACCOUNT.hex(), "active", "controller", "contract", "onboarding", None),
    )
    raw = encode(ProvisionFrame(slot=0, account_hash=ACCOUNT, account_cap=1000, hold_until_day=273))
    assert signer.sign(raw).reason == "vendor_due_diligence_unavailable"
    signer.begin_fixture_onboarding({"V-0001": OTHER_ACCOUNT.hex()})
    assert signer.sign(raw).reason == "vendor_due_diligence_unavailable"
    signer.begin_fixture_onboarding({"V-0001": ACCOUNT.hex()})
    assert signer.sign(raw).ok
    signer.end_fixture_onboarding()
    signer._con.execute(
        "INSERT INTO vendor_bank_accounts VALUES(?,?,?,?,?,?,?)",
        ("V-0002", OTHER_ACCOUNT.hex(), "active", "controller", "contract", "onboarding", None),
    )
    other = encode(
        ProvisionFrame(slot=1, account_hash=OTHER_ACCOUNT, account_cap=1000, hold_until_day=273)
    )
    assert signer.sign(other).reason == "vendor_due_diligence_unavailable"
    assert len(transport.model.vendors) == 1
    assert (
        signer.status().hardware["live_vendor_vetting"]
        == "unavailable-new-vendor-enrollment-disabled"
    )


def test_runtime_clears_fixture_authority_and_jumper_on_provisioning_failure(tmp_path, monkeypatch):
    from agents.runtime import start_runtime

    signer, _ = make_signer(tmp_path / "state.db")
    monkeypatch.setenv("PAYGUARD_HW_PROVISION", "1")
    monkeypatch.setattr("agents.runtime.bootstrap", lambda *args, **kwargs: None)

    def fail():
        assert signer._fixture_onboarding
        raise RuntimeError("simulated provisioning interruption")

    rt = SimpleNamespace(
        signer=signer,
        ledger=None,
        store=None,
        vendors=None,
        suppliers=[SimpleNamespace(id="V-0001", iban="DE02120300000000202051")],
        payment_agent=object(),
        jumper=SimpleNamespace(present=False),
        provisioner=SimpleNamespace(provision_all=fail),
    )
    with pytest.raises(RuntimeError, match="provisioning interruption"):
        start_runtime(rt)
    assert signer._fixture_onboarding == {}
    assert rt.jumper.present is False


def test_private_distinct_role_keys(tmp_path):
    path = tmp_path / "key"
    path.write_text(KEYS[0].hex())
    path.chmod(0o600)
    assert load_role_key(path) == KEYS[0]
    path.write_text(json.dumps({"admin": KEYS[0].hex()}))
    assert load_role_key(path, "admin") == KEYS[0]
    with pytest.raises(ValueError, match="different role"):
        load_role_key(path, "controller")
    path.write_text(json.dumps({"admin": KEYS[0].hex(), "response": KEYS[2].hex()}))
    with pytest.raises(ValueError, match="exactly one role"):
        load_role_key(path)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        load_role_key(path)
    with pytest.raises(ValueError, match="distinct"):
        HardwareSigner(
            tmp_path / "state.db",
            transport=ExplicitModelTransport(),
            admin_key=KEYS[0],
            controller_key=KEYS[0],
            response_key=KEYS[2],
        )


def test_explicit_partial_batch_credit_and_discount_binding():
    result = canonical_binding(
        {
            "pay_id": "PAY-000001",
            "vendor_id": "V-1",
            "account_hash": ACCOUNT.hex(),
            "amount_minor": 85,
            "currency": "EUR",
            "eur_equiv_minor": 85,
            "settlement_minor": 85,
            "settlement_currency": "EUR",
            "payable_ids": ["CREDIT", "INVOICE"],
            "covered": {"INVOICE": 100},
            "credits": {"CREDIT": -10},
            "discount_minor": 5,
        }
    )
    assert result["amount_minor"] == 85
    with pytest.raises(ValueError, match="native amount"):
        canonical_binding({**result, "amount_minor": 86})


def test_proof_tampering_rejected_even_when_legacy64_matches(tmp_path):
    signer, _ = make_signer(tmp_path / "state.db")
    signer.provision_vendor("V-0001", ACCOUNT, 10_000)
    raw, binding = request(signer)
    proof = signer.sign(raw).authorization
    changed = json.loads(json.dumps(proof))
    changed["binding"]["settlement_minor"] += 1
    with pytest.raises(ValueError):
        verify_authorization(changed, raw, KEYS[2], binding, signer.session)
    changed = dict(proof, response_mac_hex="00" * 32)
    with pytest.raises(ValueError, match="authentication"):
        verify_authorization(changed, raw, KEYS[2], binding, signer.session)
