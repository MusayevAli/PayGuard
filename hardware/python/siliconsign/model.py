"""Executable policy reference. Explicit simulation: this is not FPGA execution."""
from __future__ import annotations
import hmac
import time
from dataclasses import dataclass
from .protocol import *

@dataclass
class Vendor:
    account: bytes
    version: int
    cap: int
    spent: int = 0
    count: int = 0
    hold_until: int = 0
    enabled: bool = True

@dataclass
class Invoice:
    frame: Frame
    paid: bool = False
    approval_until: int = 0

class PolicyModel:
    def __init__(self, admin_key: bytes, controller_key: bytes, response_key: bytes,
                 clock=time.monotonic, nv=16, ni=64):
        if any(len(k)!=32 or not any(k) for k in (admin_key,controller_key,response_key)):
            raise ValueError("all keys must have 32 bytes")
        if len({admin_key,controller_key,response_key}) != 3:
            raise ValueError("role keys must be distinct")
        self.admin_key,self.controller_key,self.response_key=admin_key,controller_key,response_key
        self.clock,self.nv,self.ni=clock,nv,ni
        self.reset()
    def reset(self):
        self.initialized=False; self.locked=False; self.session=bytes(16)
        self.start_unix=0; self.start_mono=self.clock(); self.offset=0; self._frozen_now=0
        self.currency=840; self.daily_cap=0; self.threshold=0; self.cooldown=0
        self.max_count=0; self.spent=0; self.day=0; self.auth_counter=0
        self.admin_seq=self.controller_seq=self.pay_seq=0
        self.vendors={}; self.invoices={}
    @property
    def now(self):
        if not self.initialized:return self._frozen_now
        value=self.start_unix+max(0,int(self.clock()-self.start_mono))+self.offset
        if value>MAX_U64:
            self._frozen_now=MAX_U64;self.initialized=False;self.locked=True
            return MAX_U64
        return value
    def advance(self, seconds):
        """Simulation-only time travel. No corresponding hardware command exists."""
        if type(seconds) is not int or seconds<0: raise ValueError("seconds must be nonnegative integer")
        self.offset+=seconds
    def process(self, data: Frame | bytes, mac: bytes=bytes(32)):
        f=data if isinstance(data,Frame) else Frame.unpack(data)
        _=self.now  # Synchronize autonomous clock overflow before accepting a request.
        status=self._execute(f,mac)
        w=list(f.words); w[0]=RESPONSE_MAGIC; w[56]=int(status);w[57]=self.auth_counter
        w[58]=self.now&0xffffffff;w[59]=self.now>>32
        w[60]=self.spent&0xffffffff;w[61]=self.spent>>32
        w[62]=self.vendors[f.words[8]].count if f.words[8] in self.vendors else 0
        w[63]=int(self.initialized)
        r=Frame(tuple(w)).pack()
        return r,authenticate(self.response_key,r)
    def _execute(self,f,mac):
        w=f.words
        if w[0]!=COMMAND_MAGIC or w[1]!=VERSION or w[2] not in range(1,12) or w[55] or any(w[56:]): return Status.BAD_FRAME
        if w[11]&~1: return Status.FLAGS
        op=f.op
        role="admin" if op in ADMIN_OPS else "controller" if op in CONTROLLER_OPS else "pay"
        if op in ADMIN_OPS or op in CONTROLLER_OPS:
            key=self.admin_key if role=="admin" else self.controller_key
            if len(mac)!=32 or not hmac.compare_digest(mac,authenticate(key,f)): return Status.AUTH
        if op==Op.STATUS:return Status.OK
        if op==Op.INIT:
            if self.initialized or self.locked: return Status.LOCKED
            if w[3]<=self.admin_seq: return Status.SEQUENCE
            if not any(f.field("session")) or not f.field("timestamp") or not f.field("daily_cap") or not f.field("threshold") or not w[40] or not w[41]: return Status.ZERO
            if not 1<=w[10]<=999:return Status.CURRENCY
            if w[41]>65535:return Status.RANGE
            if f.field("restored_spent")>f.field("daily_cap"): return Status.DAILY_CAP
            self.session=f.field("session");self.start_unix=f.field("timestamp");self.start_mono=self.clock();self.offset=0
            self.currency=w[10];self.daily_cap=f.field("daily_cap");self.threshold=f.field("threshold");self.cooldown=w[40];self.max_count=w[41]
            self.spent=f.field("restored_spent");self.initialized=True;self.day=self.now//86400;self.admin_seq=w[3]
            return Status.OK
        if not self.initialized: return Status.LOCKED
        if f.field("session") != self.session: return Status.SESSION
        if op==Op.STATUS: return Status.OK
        last=self.admin_seq if role=="admin" else self.controller_seq if role=="controller" else self.pay_seq
        if w[3]<=last: return Status.SEQUENCE
        if op in {Op.ADD_VENDOR,Op.CHANGE_BANK,Op.DISABLE_VENDOR,Op.COMMIT_INVOICE,Op.APPROVE_EXCEPTION,Op.PAY,Op.REBIND_INVOICE} and w[8]>=self.nv: return Status.RANGE
        if op in {Op.COMMIT_INVOICE,Op.APPROVE_EXCEPTION,Op.PAY,Op.REBIND_INVOICE} and w[9]>=self.ni: return Status.RANGE
        result=self._operation(f)
        if result==Status.OK:
            if role=="admin": self.admin_seq=w[3]
            elif role=="controller": self.controller_seq=w[3]
            else: self.pay_seq=w[3]
        return result
    def _vendor(self,f):
        v=self.vendors.get(f.words[8])
        if v is None: return None,Status.VENDOR
        if not v.enabled: return None,Status.DISABLED
        return v,Status.OK
    def _binding(self,f,inv):
        a=inv.frame;v=self.vendors[f.words[8]]
        if f.words[8]!=a.words[8] or f.field("invoice_hash")!=a.field("invoice_hash"):return Status.INVOICE
        if f.field("account")!=v.account or f.field("account")!=a.field("account"):return Status.ACCOUNT
        if f.words[14]!=v.version or f.words[14]!=a.words[14]:return Status.VERSION
        if f.words[10]!=self.currency or f.words[10]!=a.words[10]:return Status.CURRENCY
        if not f.field("amount") or f.field("amount")!=a.field("amount"):return Status.AMOUNT
        if f.words[11]!=a.words[11]:return Status.FLAGS
        return Status.OK
    def _operation(self,f):
        w=f.words;op=f.op;amount=f.field("amount")
        if op==Op.LOCK:
            self._frozen_now=self.now;self.initialized=False;self.locked=True;return Status.OK
        if op==Op.ADVANCE_DAY:
            t=f.field("timestamp")
            if t>self.now or t//86400<=self.day:return Status.TIME
            self.day=t//86400;self.spent=0
            for v in self.vendors.values():v.spent=0;v.count=0
            return Status.OK
        if op==Op.ADD_VENDOR:
            if w[8] in self.vendors:return Status.EXISTS
            if not any(f.field("account")) or not any(f.field("evidence")) or not f.field("vendor_cap"):return Status.ZERO
            if w[14]!=1:return Status.VERSION
            if w[10]!=self.currency:return Status.CURRENCY
            if f.field("restored_vendor_spent")>f.field("vendor_cap"):return Status.VENDOR_CAP
            if w[54]>self.max_count:return Status.COUNT
            self.vendors[w[8]]=Vendor(f.field("account"),1,f.field("vendor_cap"),f.field("restored_vendor_spent"),w[54],hold_until=self.now)
            return Status.OK
        v,error=self._vendor(f)
        if error!=Status.OK:return error
        if op==Op.DISABLE_VENDOR:v.enabled=False;return Status.OK
        if op==Op.CHANGE_BANK:
            if not any(f.field("account")) or not any(f.field("evidence")):return Status.ZERO
            if v.version==0xffffffff:return Status.OVERFLOW
            if w[14]!=v.version+1:return Status.VERSION
            if f.field("account")==v.account:return Status.ACCOUNT
            if w[10]!=self.currency:return Status.CURRENCY
            if self.now>MAX_U64-self.cooldown:return Status.OVERFLOW
            v.account=f.field("account");v.version=w[14];v.hold_until=self.now+self.cooldown
            return Status.OK
        if op==Op.COMMIT_INVOICE:
            if w[9] in self.invoices:return Status.EXISTS
            if any(i.frame.field("invoice_hash")==f.field("invoice_hash") for i in self.invoices.values()):return Status.DUPLICATE
            if f.field("account")!=v.account:return Status.ACCOUNT
            if w[14]!=v.version:return Status.VERSION
            if w[10]!=self.currency:return Status.CURRENCY
            if not amount:return Status.AMOUNT
            if not any(f.field("invoice_hash")) or not any(f.field("evidence")):return Status.ZERO
            self.invoices[w[9]]=Invoice(f)
            return Status.OK
        inv=self.invoices.get(w[9])
        if inv is None:return Status.INVOICE
        if inv.paid:return Status.PAID
        if op==Op.REBIND_INVOICE:
            a=inv.frame
            if f.words[8]!=a.words[8] or f.field("invoice_hash")!=a.field("invoice_hash"):return Status.INVOICE
            if f.field("account")!=v.account:return Status.ACCOUNT
            if w[14]!=v.version:return Status.VERSION
            if w[10]!=self.currency or w[10]!=a.words[10]:return Status.CURRENCY
            if not amount or amount!=a.field("amount"):return Status.AMOUNT
            if w[11]!=a.words[11]:return Status.FLAGS
            if not any(f.field("evidence")):return Status.ZERO
            if f.field("account")==a.field("account") and w[14]==a.words[14]:return Status.ACCOUNT
            inv.frame=f.replace(op=Op.COMMIT_INVOICE);inv.approval_until=0
            return Status.OK
        error=self._binding(f,inv)
        if error!=Status.OK:return error
        if op==Op.APPROVE_EXCEPTION:
            if not 1<=w[15]<=300:return Status.RANGE
            if self.now>MAX_U64-w[15]:return Status.OVERFLOW
            inv.approval_until=self.now+w[15];return Status.OK
        if op==Op.PAY:
            if self.now//86400!=self.day:return Status.DAY_ROLLOVER
            if self.now<v.hold_until:return Status.COOLDOWN
            if amount>MAX_U64-self.spent or amount>MAX_U64-v.spent:return Status.OVERFLOW
            if self.spent+amount>self.daily_cap:return Status.DAILY_CAP
            if v.spent+amount>v.cap:return Status.VENDOR_CAP
            if v.count>=self.max_count:return Status.COUNT
            if (inv.frame.words[11]&1 or v.spent+amount>self.threshold) and (not inv.approval_until or self.now>=inv.approval_until):return Status.APPROVAL
            if self.auth_counter==0xffffffff:return Status.OVERFLOW
            inv.paid=True;inv.approval_until=0;v.spent+=amount;v.count+=1;self.spent+=amount;self.auth_counter+=1
            return Status.OK
        return Status.BAD_FRAME
