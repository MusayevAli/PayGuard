"""Canonical fixed-width SiliconSign protocol. No float amounts or implicit truncation."""
from __future__ import annotations
import hashlib
import hmac
import json
import struct
from dataclasses import dataclass
from enum import IntEnum

COMMAND_MAGIC = 0x53494C43
RESPONSE_MAGIC = 0x53494C52
VERSION = 1
FRAME_BYTES = 256
HELLO_REQUEST = {"hello": "siliconsign.v1"}
MAX_U64 = (1 << 64) - 1

class Op(IntEnum):
    INIT = 1
    ADD_VENDOR = 2
    CHANGE_BANK = 3
    COMMIT_INVOICE = 4
    APPROVE_EXCEPTION = 5
    PAY = 6
    DISABLE_VENDOR = 7
    ADVANCE_DAY = 8
    STATUS = 9
    LOCK = 10
    REBIND_INVOICE = 11

class Status(IntEnum):
    OK=0; BAD_FRAME=1; AUTH=2; LOCKED=3; SESSION=4; SEQUENCE=5
    RANGE=6; EXISTS=7; VENDOR=8; ACCOUNT=9; INVOICE=10; AMOUNT=11
    CURRENCY=12; COOLDOWN=13; DAILY_CAP=14; VENDOR_CAP=15; APPROVAL=16
    PAID=17; CAPACITY=18; TIME=19; FLAGS=20; DISABLED=21; VERSION=22
    ZERO=23; COUNT=24; DAY_ROLLOVER=25; DUPLICATE=26; OVERFLOW=27

ADMIN_OPS = frozenset({Op.INIT, Op.ADD_VENDOR, Op.CHANGE_BANK, Op.DISABLE_VENDOR,
                       Op.ADVANCE_DAY, Op.LOCK})
CONTROLLER_OPS = frozenset({Op.COMMIT_INVOICE, Op.APPROVE_EXCEPTION, Op.REBIND_INVOICE})
FIELD_WORDS = {
    "seq":(3,1), "vendor":(8,1), "invoice":(9,1), "currency":(10,1),
    "flags":(11,1), "amount":(12,2), "vendor_version":(14,1), "ttl":(15,1),
    "timestamp":(32,2), "vendor_cap":(34,2), "daily_cap":(36,2),
    "threshold":(38,2), "cooldown":(40,1), "max_count":(41,1),
    "restored_spent":(42,2), "restored_vendor_spent":(44,2),
    "restored_count":(54,1),
}
BYTES_FIELDS = {"session":(4,16), "account":(16,32), "invoice_hash":(24,32), "evidence":(46,32)}

@dataclass(frozen=True)
class Frame:
    words: tuple[int, ...]
    def __post_init__(self):
        if len(self.words) != 64 or any(type(w) is not int or w < 0 or w > 0xffffffff for w in self.words):
            raise ValueError("frame must contain exactly 64 unsigned 32-bit words")
    @classmethod
    def unpack(cls, data: bytes) -> "Frame":
        if len(data) != FRAME_BYTES:
            raise ValueError("frame must be exactly 256 bytes")
        return cls(struct.unpack("<64I", data))
    def pack(self) -> bytes:
        return struct.pack("<64I", *self.words)
    def integer(self, start: int, count: int = 1) -> int:
        return sum(self.words[start+i] << (32*i) for i in range(count))
    def blob(self, start: int, length: int) -> bytes:
        return self.pack()[start*4:start*4+length]
    def field(self, name: str):
        if name in FIELD_WORDS:
            return self.integer(*FIELD_WORDS[name])
        return self.blob(*BYTES_FIELDS[name])
    @property
    def op(self) -> Op:
        return Op(self.words[2])
    @property
    def status(self) -> Status:
        return Status(self.words[56])
    def replace(self, **fields) -> "Frame":
        words = list(self.words)
        if "op" in fields:
            words[2] = int(fields.pop("op"))
        for name, value in fields.items():
            if name in FIELD_WORDS:
                start, count = FIELD_WORDS[name]
                if type(value) is not int or not 0 <= value < (1 << (32*count)):
                    raise ValueError(f"{name} is out of unsigned range")
                for i in range(count):
                    words[start+i] = (value >> (32*i)) & 0xffffffff
            elif name in BYTES_FIELDS:
                start, length = BYTES_FIELDS[name]
                if not isinstance(value, bytes) or len(value) != length:
                    raise ValueError(f"{name} must be {length} bytes")
                words[start:start+length//4] = struct.unpack("<"+"I"*(length//4), value)
            else:
                raise ValueError(f"unknown frame field {name}")
        return Frame(tuple(words))

def command(op: Op, **fields) -> Frame:
    words = [0]*64
    words[0], words[1], words[2], words[10] = COMMAND_MAGIC, VERSION, int(op), 840
    return Frame(tuple(words)).replace(**fields)

def digest(text: str | bytes) -> bytes:
    return hashlib.sha256(text.encode() if isinstance(text,str) else text).digest()

def authenticate(key: bytes, data: Frame | bytes) -> bytes:
    if len(key) != 32:
        raise ValueError("HMAC key must be exactly 32 bytes")
    return hmac.digest(key, data.pack() if isinstance(data,Frame) else data, "sha256")

def envelope(frame: Frame | bytes, mac: bytes = bytes(32)) -> bytes:
    data = frame.pack() if isinstance(frame,Frame) else frame
    if len(data) != 256 or len(mac) != 32:
        raise ValueError("invalid envelope length")
    return (json.dumps({"frame":data.hex(), "mac":mac.hex()}, separators=(",",":"))+"\n").encode()

def parse_envelope(line: bytes) -> tuple[bytes, bytes]:
    if len(line) > 2048:
        raise ValueError("oversized envelope")
    obj=json.loads(line)
    if not isinstance(obj,dict) or set(obj) != {"frame","mac"}:
        raise ValueError("envelope requires exactly frame and mac")
    data, mac = bytes.fromhex(obj["frame"]), bytes.fromhex(obj["mac"])
    if len(data) != 256 or len(mac) != 32:
        raise ValueError("wrong frame or MAC length")
    return data,mac

def describe(data: Frame | bytes) -> dict:
    f=data if isinstance(data,Frame) else Frame.unpack(data)
    return {"op":Op(f.words[2]).name,"seq":f.words[3],"vendor":f.words[8],
            "invoice":f.words[9],"amount_cents":f.integer(12,2),
            "session":f.blob(4,16).hex(),"status":Status(f.words[56]).name,
            "authorization_id":f.words[57],"hardware_time":f.integer(58,2),
            "daily_reserved_cents":f.integer(60,2),"vendor_count":f.words[62],
            "initialized":bool(f.words[63])}
