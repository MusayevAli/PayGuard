"""Read-only classification of recorded chip evidence for the dashboard lamp.

No status/transport calls are made. The caller supplies the most recent preceding
payment_requested payload for this pay_id, retained by the event projection.
Only an authenticated, independently bound PAY success is green. An authenticated
COMMIT_INVOICE/PAY refusal is red; host refusals and missing proof are unverified.
Keys and raw proof bytes are never returned to the browser.
"""
from __future__ import annotations

import hmac
from typing import Any

# isort: off
# authority establishes the maintained FPGA client's import path.
from signer.authority import (
    PROOF_SCHEMA,
    canonical_binding,
    evidence_digest,
    invoice_digest,
    validate_legacy_binding,
    verify_authorization,
)
from siliconsign.protocol import COMMAND_MAGIC, RESPONSE_MAGIC, VERSION, Frame, Op, Status, authenticate
from sim.models import Event
# isort: on

PROOF_FIELDS = {'schema', 'command_hex', 'response_hex', 'response_mac_hex',
                'legacy_frame_hex', 'binding'}


def _hex(value: Any, size: int) -> bytes:
    if not isinstance(value, str) or len(value) != size * 2:
        raise ValueError('incorrect hex field length')
    decoded = bytes.fromhex(value)
    if len(decoded) != size:
        raise ValueError('incorrect decoded field length')
    return decoded


def _recorded_response(data, signer, expected_request):
    proof = data.get('authorization')
    if not isinstance(proof, dict) or set(proof) != PROOF_FIELDS or proof['schema'] != PROOF_SCHEMA:
        raise ValueError('invalid proof envelope')
    if not isinstance(expected_request, dict):
        raise ValueError('independent request is missing')
    binding = canonical_binding(expected_request)
    if binding != canonical_binding(proof['binding']) or binding['pay_id'] != data.get('pay_id'):
        raise ValueError('independent payment binding differs')
    legacy = _hex(expected_request.get('frame_hex'), 64)
    if legacy != _hex(proof['legacy_frame_hex'], 64):
        raise ValueError('legacy request differs')
    validate_legacy_binding(legacy, binding)
    command = Frame.unpack(_hex(proof['command_hex'], 256))
    response = Frame.unpack(_hex(proof['response_hex'], 256))
    mac = _hex(proof['response_mac_hex'], 32)
    if _hex(data.get('raw_hex'), 256) != response.pack():
        raise ValueError('event response differs from proof')
    key, session = signer.response_key, signer.session
    if not isinstance(key, bytes) or len(key) != 32 or not any(key):
        raise ValueError('response key unavailable')
    if not isinstance(session, bytes) or len(session) != 16 or not any(session):
        raise ValueError('current session unavailable')
    if not hmac.compare_digest(mac, authenticate(key, response)):
        raise ValueError('response authentication failed')
    if (command.words[:2] != (COMMAND_MAGIC, VERSION)
            or command.op not in (Op.COMMIT_INVOICE, Op.PAY)
            or response.words[0] != RESPONSE_MAGIC
            or response.words[1:56] != command.words[1:56]
            or command.field('session') != session):
        raise ValueError('command, response or session differs')
    if (command.field('account') != bytes.fromhex(binding['account_hash'])
            or command.field('amount') != binding['eur_equiv_minor']
            or command.words[10] != 978
            or command.field('invoice_hash') != invoice_digest(binding)
            or command.field('evidence') != evidence_digest(legacy, binding)):
        raise ValueError('response is not bound to the independent payment')
    _ = response.status  # Reject an unknown device status code.
    return proof, legacy, command, response, key, session


def authorization_indicator(event: Event, signer: Any,
                            expected_request: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return state/label/reason/op/status/pay_id plus immutable event metadata.

    ``software`` identifies a stub or explicitly marked model transport. ``unverified``
    never claims that the FPGA refused: it includes host-only errors, stale sessions,
    missing independent requests and malformed/tampered evidence. Verification uses
    the existing response key in memory and does not contact the live device.
    """
    data = event.payload
    result: dict[str, Any] = {
        'state': 'unverified', 'label': 'FPGA outcome unverified', 'reason': 'missing_hardware_proof',
        'op': None, 'status': None, 'status_code': None, 'pay_id': data.get('pay_id'),
        'proof_verified': False, 'authorization_counter': None,
        'event_id': event.id, 'seq': event.seq, 'event_time': event.event_time.isoformat(),
    }
    if event.type != 'chip_response':
        return {**result, 'reason': 'not_chip_response'}
    if data.get('signer') == 'stub' or getattr(signer, 'kind', None) == 'stub':
        return {**result, 'state': 'software', 'label': 'Software signer', 'reason': 'software_signer'}
    if data.get('signer') != 'hw' or getattr(signer, 'kind', None) != 'hw':
        return {**result, 'reason': 'hardware_source_unconfirmed'}
    transport = getattr(signer, 'transport', None)
    if getattr(transport, 'is_hardware', None) is False:
        return {**result, 'state': 'software', 'label': 'Software model', 'reason': 'model_transport'}
    if getattr(transport, 'is_hardware', None) is not True:
        return {**result, 'reason': 'hardware_transport_unconfirmed'}
    if not isinstance(data.get('authorization'), dict):
        return result
    if not isinstance(expected_request, dict):
        return {**result, 'reason': 'missing_payment_request'}
    try:
        proof, legacy, command, response, key, session = _recorded_response(data, signer, expected_request)
        if data.get('ok') is True:
            verified = verify_authorization(proof, legacy, key, expected_request, session)
            return {**result, 'state': 'approved', 'label': 'FPGA authorized payment',
                    'reason': 'authenticated_pay', 'op': Op.PAY.name, 'status': Status.OK.name,
                    'status_code': int(Status.OK), 'proof_verified': True,
                    'authorization_counter': verified.words[57]}
        if data.get('ok') is False and response.status != Status.OK:
            return {**result, 'state': 'blocked', 'label': 'FPGA refused payment',
                    'reason': response.status.name.lower(), 'op': command.op.name,
                    'status': response.status.name, 'status_code': int(response.status),
                    'proof_verified': True}
        return {**result, 'reason': 'outcome_is_not_pay_authorization_or_refusal'}
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return {**result, 'reason': 'invalid_hardware_proof'}
