from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

# isort: off
# Import the dashboard helper before the vendored protocol it makes available.
from api.authorization import authorization_indicator
from signer.authority import PROOF_SCHEMA, evidence_digest, invoice_digest
from signer.frames import PayFrame, encode
from siliconsign.protocol import RESPONSE_MAGIC, Frame, Op, Status, authenticate, command
from sim.models import Event
# isort: on

KEY, SESSION = b'r' * 32, b's' * 16


class NoIOTransport:
    is_hardware = True

    def exchange(self, *args):
        pytest.fail('Indicator must never access hardware')

    def info(self):
        pytest.fail('Indicator must never read hardware status')


def signer():
    return SimpleNamespace(kind='hw', response_key=KEY, session=SESSION, transport=NoIOTransport())


def evidence(op=Op.PAY, status=Status.OK, *, ok=None, counter=7):
    account = hashlib.sha256(b'approved fixture account').digest()
    legacy = encode(PayFrame(seq=1, account_hash=account, amount_minor=12345,
                             currency='EUR', nonce=0, pay_id='PAY-000001'))
    request = {'pay_id': 'PAY-000001', 'vendor_id': 'V-0001', 'account_hash': account.hex(),
               'amount_minor': 12345, 'currency': 'EUR', 'eur_equiv_minor': 12345,
               'settlement_currency': 'EUR', 'settlement_minor': 12345,
               'payable_ids': ['AP-1'], 'covered': {'AP-1': 12345}, 'credits': {},
               'discount_minor': 0, 'fx_rate': '1', 'rate_date': '2026-10-25'}
    cmd = command(op, seq=1, session=SESSION, vendor=0, invoice=0, currency=978,
                  amount=12345, account=account, vendor_version=1,
                  invoice_hash=invoice_digest(request), evidence=evidence_digest(legacy, request))
    words = list(cmd.words)
    words[0], words[56], words[57], words[63] = RESPONSE_MAGIC, int(status), counter, 1
    response = Frame(tuple(words))
    proof = {'schema': PROOF_SCHEMA, 'command_hex': cmd.pack().hex(),
             'response_hex': response.pack().hex(), 'response_mac_hex': authenticate(KEY, response).hex(),
             'legacy_frame_hex': legacy.hex(), 'binding': request.copy()}
    now = datetime(2026, 10, 25, tzinfo=UTC)
    event = Event(id='ev_abcdef12', seq=777, type='chip_response', event_time=now, known_at=now,
                  source='signer', actor='hw', payload={
                      'pay_id': request['pay_id'], 'ok': status == Status.OK if ok is None else ok,
                      'tag_hex': None, 'reasons': [], 'chip_reason': None, 'latency_ms': 2,
                      'raw_hex': response.pack().hex(), 'signer': 'hw', 'authorization': proof})
    return event, {**request, 'frame_hex': legacy.hex()}


def test_green_requires_a_verified_bound_pay_without_io_or_key_disclosure():
    event, request = evidence()
    result = authorization_indicator(event, signer(), request)
    assert result['state'] == 'approved' and result['proof_verified'] is True
    assert result['op'] == 'PAY' and result['status'] == 'OK'
    assert result['authorization_counter'] == 7
    assert result['event_id'] == event.id and result['seq'] == 777
    assert KEY.hex() not in json.dumps(result)
    assert 'response_mac_hex' not in result and 'command_hex' not in result


@pytest.mark.parametrize('op,status', [(Op.COMMIT_INVOICE, Status.ACCOUNT),
                                      (Op.PAY, Status.APPROVAL), (Op.PAY, Status.DAILY_CAP)])
def test_red_comes_from_authenticated_chip_refusal_including_commit(op, status):
    event, request = evidence(op, status)
    result = authorization_indicator(event, signer(), request)
    assert result['state'] == 'blocked' and result['proof_verified'] is True
    assert result['op'] == op.name and result['status'] == status.name
    assert result['reason'] == status.name.lower()
    assert result['authorization_counter'] is None


@pytest.mark.parametrize('op', [Op.COMMIT_INVOICE, Op.STATUS, Op.APPROVE_EXCEPTION])
def test_successful_nonpay_acknowledgment_never_turns_green(op):
    event, request = evidence(op, Status.OK, counter=99)
    assert authorization_indicator(event, signer(), request)['state'] == 'unverified'


@pytest.mark.parametrize('reason', ['hardware_unavailable', 'not_allowlisted', 'bad_nonce'])
def test_host_refusal_without_proof_is_not_claimed_as_chip_refusal(reason):
    event, request = evidence(status=Status.ACCOUNT)
    event.payload.update(authorization=None, raw_hex='', chip_reason=reason)
    result = authorization_indicator(event, signer(), request)
    assert result['state'] == 'unverified' and result['proof_verified'] is False


def test_stub_and_explicit_model_are_never_physical_green():
    event, request = evidence()
    event.payload['signer'] = 'stub'
    assert authorization_indicator(event, signer(), request)['state'] == 'software'
    event.payload['signer'] = 'hw'
    model = signer()
    model.transport.is_hardware = False
    assert authorization_indicator(event, model, request)['state'] == 'software'


@pytest.mark.parametrize('mutation', ['mac', 'raw', 'command', 'schema', 'binding', 'legacy',
                                    'missing_field', 'long_hex', 'wrong_pay_id'])
@pytest.mark.parametrize('status', [Status.OK, Status.ACCOUNT])
def test_malformed_or_tampered_proof_is_unverified(mutation, status):
    event, request = evidence(status=status)
    proof = event.payload['authorization']
    if mutation == 'mac':
        proof['response_mac_hex'] = '00' * 32
    elif mutation == 'raw':
        event.payload['raw_hex'] = '00' * 256
    elif mutation == 'command':
        proof['command_hex'] = '00' * 256
    elif mutation == 'schema':
        proof['schema'] = 'unknown'
    elif mutation == 'binding':
        proof['binding'] = {**proof['binding'], 'vendor_id': 'OTHER'}
    elif mutation == 'legacy':
        proof['legacy_frame_hex'] = '00' * 64
    elif mutation == 'missing_field':
        del proof['response_mac_hex']
    elif mutation == 'long_hex':
        proof['response_hex'] = '0' * 10000
    else:
        event.payload['pay_id'] = 'OTHER'
    result = authorization_indicator(event, signer(), request)
    assert result['state'] == 'unverified' and result['proof_verified'] is False


@pytest.mark.parametrize('status', [Status.OK, Status.ACCOUNT])
def test_independent_request_and_current_session_are_required(status):
    event, request = evidence(status=status)
    assert authorization_indicator(event, signer())['state'] == 'unverified'
    changed = {**request, 'vendor_id': 'OTHER'}
    assert authorization_indicator(event, signer(), changed)['state'] == 'unverified'
    device = signer()
    device.session = b'n' * 16
    assert authorization_indicator(event, device, request)['state'] == 'unverified'


def test_authentic_but_mismatched_response_or_success_metadata_is_rejected():
    for word, value in ((2, int(Op.COMMIT_INVOICE)), (0, 0), (56, 99), (63, 0), (57, 0)):
        event, request = evidence()
        proof = event.payload['authorization']
        words = list(Frame.unpack(bytes.fromhex(proof['response_hex'])).words)
        words[word] = value
        response = Frame(tuple(words))
        proof['response_hex'] = response.pack().hex()
        proof['response_mac_hex'] = authenticate(KEY, response).hex()
        event.payload['raw_hex'] = proof['response_hex']
        assert authorization_indicator(event, signer(), request)['state'] == 'unverified'


def test_indicator_does_not_mutate_event_request_or_signer_state():
    event, request = evidence()
    original_event, original_request = copy.deepcopy(event), copy.deepcopy(request)
    authorization_indicator(event, signer(), request)
    assert event == original_event and request == original_request
