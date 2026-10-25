"""Durable trusted bridge and conservative restart recovery.

A boot never silently starts a new budget. Hardware reservations, including issued
but unsettled authorizations, survive in the journal. Missing results require human
reconciliation. The agent-facing method accepts PAY and STATUS only; authenticated
management uses the trusted coordinator explicitly.
"""
from __future__ import annotations
import secrets
import threading
import time
from .protocol import *
from .gateway import Gateway, Journal, GatewayError, RecoveryRequired

class JournaledDevice:
    def __init__(self,transport,journal:Journal,gateway:Gateway):
        self.transport,self.journal,self.gateway=transport,journal,gateway
        self.locked=True
        self.mutex=threading.RLock()
    def transact(self,frame:Frame,mac:bytes=bytes(32),*,trusted=False):
        with self.mutex:
            if not trusted and (self.locked or frame.op not in {Op.PAY,Op.STATUS}):
                raise RecoveryRequired("agent path locked or operation requires separate trusted role")
            try:
                intent=self.journal.begin(frame.pack(),mac)
                response,signature=self.transport.exchange(frame.pack(),mac)
                self.journal.finish(intent,response,signature)
                self.transport.acknowledge()
            except BaseException:
                self.locked=True
                raise
            return response,signature
    def pay(self,frame:Frame):
        if frame.op!=Op.PAY:raise ValueError("PAY command required")
        with self.mutex:
            response,signature=self.transact(frame)
            result=Frame.unpack(response)
            receipt=None
            if result.status==Status.OK:
                try:receipt=self.gateway.settle(frame.pack(),response,signature)
                except BaseException:
                    self.locked=True
                    raise
            return response,signature,receipt
    def reconcile_pending_from_hardware(self):
        """Only recovers a still-latched authentic response; never guesses execution."""
        with self.mutex:
            pending=self.journal.pending()
            if len(pending)!=1:raise RecoveryRequired("exactly one unresolved intent required")
            if not hasattr(self.transport,"pending_response"):
                raise RecoveryRequired("transport cannot recover latched FPGA response")
            response,signature=self.transport.pending_response()
            self.journal.finish(pending[0]["id"],response,signature)
            self.transport.acknowledge()
            # Remains locked until full reprovisioning/reconciliation.

class RecoveryCoordinator:
    def __init__(self,device:JournaledDevice,admin_key:bytes,controller_key:bytes):
        self.device=device;self.admin_key=admin_key;self.controller_key=controller_key
        self.session=None;self.admin_seq=0;self.controller_seq=0
    def _send(self,frame):
        role="admin" if frame.op in ADMIN_OPS else "controller"
        if role=="admin":self.admin_seq+=1;seq=self.admin_seq;key=self.admin_key
        else:self.controller_seq+=1;seq=self.controller_seq;key=self.controller_key
        f=frame.replace(seq=seq,session=self.session)
        response,signature=self.device.transact(f,authenticate(key,f),trusted=True)
        if Frame.unpack(response).status!=Status.OK:
            self.device.locked=True
            raise RecoveryRequired(f"provisioning {frame.op.name} failed: {Frame.unpack(response).status.name}")
        if f.op in {Op.COMMIT_INVOICE,Op.REBIND_INVOICE}:
            try:self.device.gateway.recognize(f.pack())
            except BaseException:
                self.device.locked=True
                raise
        return f,response,signature
    def start_new(self,*,timestamp=None,daily_cap=10_000_000,threshold=500_000,
                  cooldown=86400,max_count=100,currency=840):
        with self.device.mutex:
            if self.device.journal.records():raise RecoveryRequired("existing journal requires recover(), not fresh initialization")
            self.device.gateway.revoke_active_session();self.device.locked=True
            self.session=secrets.token_bytes(16)
            self._send(command(Op.INIT,timestamp=int(time.time()) if timestamp is None else timestamp,
                               daily_cap=daily_cap,threshold=threshold,cooldown=cooldown,
                               max_count=max_count,currency=currency))
            self.device.gateway.activate_session(self.session)
            self.device.locked=False
            return self.session
    def manage(self,frame:Frame):
        """Trusted admin/controller entrypoint; never expose its keys to the agent."""
        with self.device.mutex:
            if self.session is None or self.device.locked:raise RecoveryRequired("coordinator has no active recovered session")
            if frame.op==Op.REBIND_INVOICE:
                for row in self.device.journal.records():
                    old=Frame.unpack(row["command"])
                    if old.op==Op.PAY and Frame.unpack(row["response"]).status==Status.OK and old.field("invoice_hash")==frame.field("invoice_hash"):
                        raise RecoveryRequired("cannot rebind an issued authorization, even if unsettled")
            return self._send(frame)
    def snapshot(self):
        self.device.journal.assert_recovery_complete()
        self.device.journal.assert_recoverable()
        records=self.device.journal.records()
        state=None;spent_ids=set();max_time=0
        for row in records:
            f=Frame.unpack(row["command"]);r=Frame.unpack(row["response"])
            max_time=max(max_time,r.integer(58,2))
            if r.status!=Status.OK:continue
            if f.op==Op.PAY:spent_ids.add(f.field("invoice_hash"))
            if f.op==Op.INIT:
                state={"init":f,"session":f.field("session"),"day":f.field("timestamp")//86400,
                       "spent":f.field("restored_spent"),"vendors":{},"invoices":{},"locked":False}
            if state is None or f.field("session")!=state["session"]:continue
            if f.op==Op.ADD_VENDOR:
                state["vendors"][f.words[8]]={"initial":f,"changes":[],"disabled":False,
                    "spent":f.field("restored_vendor_spent"),"count":f.words[54]}
            elif f.op==Op.CHANGE_BANK:state["vendors"][f.words[8]]["changes"].append(f)
            elif f.op==Op.DISABLE_VENDOR:state["vendors"][f.words[8]]["disabled"]=True
            elif f.op in {Op.COMMIT_INVOICE,Op.REBIND_INVOICE}:state["invoices"][f.words[9]]=f.replace(op=Op.COMMIT_INVOICE)
            elif f.op==Op.PAY:
                state["spent"]=r.integer(60,2)
                v=state["vendors"][f.words[8]];v["spent"]+=f.field("amount");v["count"]+=1
            elif f.op==Op.ADVANCE_DAY:
                state["day"]=f.field("timestamp")//86400;state["spent"]=0
                for v in state["vendors"].values():v["spent"]=0;v["count"]=0
            elif f.op==Op.LOCK:state["locked"]=True
        if state is None:raise RecoveryRequired("no completed initialization to recover")
        state["consumed"]=spent_ids;state["max_time"]=max_time
        return state
    def recover(self,*,timestamp=None,allow_operator_unlock=False):
        """Hardware must already be freshly reset by trusted operator.

        Restoration retains all successful authorization reservations, not merely bank
        receipts. Bank changes receive a fresh full wait (conservative). Exception
        approvals expire on recovery. Paid invoices are not reprovisioned. A LOCK
        requires explicit operator acknowledgement. Any unknown intent blocks recovery.
        """
        with self.device.mutex:
            self.device.locked=True
            old=self.device.gateway.active_session
            self.device.gateway.revoke_active_session()
            state=self.snapshot()
            if state["locked"] and not allow_operator_unlock:
                raise RecoveryRequired("operator LOCK recorded; explicit operator unlock required")
            now=max(int(time.time()) if timestamp is None else timestamp,state["max_time"])
            same_day=now//86400==state["day"]
            self.session=secrets.token_bytes(16);self.admin_seq=self.controller_seq=0
            self.device.journal.begin_recovery(old,self.session,{"source_session":state["session"].hex(),"vendor_count":len(state["vendors"]),"invoice_count":len(state["invoices"])})
            init=state["init"].replace(timestamp=now,restored_spent=state["spent"] if same_day else 0)
            self._send(init)
            for slot,v in sorted(state["vendors"].items()):
                self._send(v["initial"].replace(restored_vendor_spent=v["spent"] if same_day else 0,
                                               restored_count=v["count"] if same_day else 0))
                for change in v["changes"]:self._send(change)
                if v["disabled"]:self._send(command(Op.DISABLE_VENDOR,vendor=slot))
            for slot,f in sorted(state["invoices"].items()):
                if f.field("invoice_hash") in state["consumed"]:continue
                v=state["vendors"].get(f.words[8])
                if not v or v["disabled"]:continue
                latest=v["changes"][-1] if v["changes"] else v["initial"]
                if f.field("account")!=latest.field("account") or f.words[14]!=latest.words[14]:continue
                self._send(f)
            self.device.journal.checkpoint(old,self.session,{"restored_spent":state["spent"] if same_day else 0,
                "consumed_invoice_count":len(state["consumed"]),"bank_holds":"restarted in full","exception_approvals":"expired"})
            self.device.gateway.activate_session(self.session)
            self.device.locked=False
            return self.session

def submit_envelope(coordinator:RecoveryCoordinator,frame:Frame,mac:bytes):
    """Receive agent requests or already-signed human/admin capabilities.

    No signing key is exposed. Hardware verifies management authority; independently
    authenticated results authorize the trusted gateway's corresponding transition.
    """
    device=coordinator.device
    with device.mutex:
        if device.locked:raise RecoveryRequired("bridge is locked pending recovery")
        if frame.op==Op.PAY:return device.pay(frame)[:2]
        if frame.op==Op.STATUS:return device.transact(frame,mac)
        if frame.op==Op.INIT:raise RecoveryRequired("initialization is local trusted provisioning only")
        issued=False
        if frame.op==Op.REBIND_INVOICE:
            for row in device.journal.records():
                old=Frame.unpack(row["command"])
                if old.op==Op.PAY and Frame.unpack(row["response"]).status==Status.OK and old.field("invoice_hash")==frame.field("invoice_hash"):
                    issued=True;break
            if issued:raise RecoveryRequired("cannot rebind a previously issued authorization")
        response,signature=device.transact(frame,mac,trusted=True)
        if Frame.unpack(response).status==Status.OK:
            try:
                if frame.op in {Op.COMMIT_INVOICE,Op.REBIND_INVOICE}:
                    device.gateway.recognize(frame.pack(),authorization_issued=issued)
                if frame.op in ADMIN_OPS:coordinator.admin_seq=max(coordinator.admin_seq,frame.words[3])
                if frame.op in CONTROLLER_OPS:coordinator.controller_seq=max(coordinator.controller_seq,frame.words[3])
                if frame.op==Op.LOCK:
                    device.gateway.revoke_active_session();device.locked=True
            except BaseException:
                device.locked=True;raise
        return response,signature
