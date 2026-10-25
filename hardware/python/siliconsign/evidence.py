"""Reviewable evidence packets. External checks must be supplied by trusted integrations."""
from __future__ import annotations
import hashlib
import json
from .protocol import Frame, Op

def canonical_bytes(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode()

def make_packet(frame:Frame, sources:dict, *, simulated=False):
    if not isinstance(sources,dict):raise ValueError("sources must be a JSON object")
    packet={"schema":"siliconsign.evidence.v1","simulated":bool(simulated),
        "action":frame.op.name,"invoice_id_hash":frame.field("invoice_hash").hex(),
        "vendor_slot":frame.words[8],"vendor_version":frame.words[14],
        "recipient_account_hash":frame.field("account").hex(),
        "amount_cents":frame.field("amount"),"currency":frame.words[10],
        "requires_exception":bool(frame.words[11]&1),
        "source_documents":sources,
        "verification_boundary":"Provider results and bank-account ownership must be verified by a trusted integration/human. This package does not perform live CRS, sanctions, identity, or bank ownership calls.",
        "approval_scope":"Only the exact invoice, account, amount, currency and vendor version in the signed command; never overrides limits or a bank-change hold."}
    return packet,hashlib.sha256(canonical_bytes(packet)).digest()

def vetting_missing(sources):
    """Completeness check only, NOT verification of provider claims or report authenticity."""
    required={"business_credit":"reviewed","sanctions":"clear","identity":"verified","bank_ownership":"verified_out_of_band"}
    missing=[]
    for check,status in required.items():
        item=sources.get(check,{})
        if not isinstance(item,dict) or item.get("status")!=status or not item.get("report_id") or not item.get("checked_at"):
            missing.append(check)
    return missing
