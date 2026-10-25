"""Policy, durable bridge and recovery contract tests using the explicit reference model."""
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest

from siliconsign.protocol import *
from siliconsign.model import PolicyModel
from siliconsign.demo import DEMO_KEYS, run_demo
from siliconsign.gateway import Gateway, Journal, RecoveryRequired, GatewayError
from siliconsign.bridge import JournaledDevice, RecoveryCoordinator, submit_envelope
from siliconsign.transport import ModelTransport, TransportError
from siliconsign.evidence import make_packet,canonical_bytes,vetting_missing

NOW=1_700_006_400

class Fixture:
    def __init__(self,path):
        self.k=DEMO_KEYS
        self.model=PolicyModel(self.k['admin'],self.k['controller'],self.k['response'],clock=lambda:0)
        self.bank=Gateway(Path(path)/'bank.sqlite',self.k['response'])
        self.journal=Journal(Path(path)/'journal.sqlite',self.k['response'])
        self.device=JournaledDevice(ModelTransport(self.model),self.journal,self.bank)
        self.c=RecoveryCoordinator(self.device,self.k['admin'],self.k['controller'])
        self.c.start_new(timestamp=NOW,daily_cap=100_000,threshold=50_000,cooldown=60,max_count=3)
        self.account=digest('vendor account')
        self.c.manage(command(Op.ADD_VENDOR,vendor=0,account=self.account,vendor_version=1,
            vendor_cap=80_000,evidence=digest('vendor evidence')))
        self.payseq=0
    def commit(self,slot=0,amount=10_000,flags=0):
        v=self.model.vendors[0]
        f=command(Op.COMMIT_INVOICE,vendor=0,invoice=slot,amount=amount,flags=flags,
                  account=v.account,vendor_version=v.version,invoice_hash=digest('invoice '+str(slot)),
                  evidence=digest('invoice evidence '+str(slot)))
        return self.c.manage(f)[0]
    def pay(self,f):
        self.payseq+=1
        f=f.replace(op=Op.PAY,seq=self.payseq,session=self.c.session)
        result=self.device.pay(f)
        return Frame.unpack(result[0]).status,f,result
    def raw(self,f,key=None):
        data,mac=self.model.process(f,authenticate(self.k[key],f) if key else bytes(32))
        return Frame.unpack(data).status

class ProtocolTests(unittest.TestCase):
    def test_roundtrip_and_word_order(self):
        f=command(Op.PAY,amount=0x0102030405060708,session=bytes(range(16)),account=bytes(range(32)))
        self.assertEqual(Frame.unpack(f.pack()),f)
        self.assertEqual(f.pack()[48:56],bytes.fromhex('0807060504030201'))
        self.assertEqual(parse_envelope(envelope(f))[0],f.pack())
    def test_rejects_float_bool_negative_overflow(self):
        for value in [1.2,True,-1,1<<64]:
            with self.assertRaises(ValueError):command(Op.PAY,amount=value)
    def test_envelope_strict(self):
        for line in [b'{}',b'x'*2049,b'{"frame":"00","mac":"00"}']:
            with self.assertRaises((ValueError,json.JSONDecodeError)):parse_envelope(line)
    def test_bad_key_roles_rejected(self):
        k=DEMO_KEYS
        with self.assertRaises(ValueError):PolicyModel(k['admin'],k['admin'],k['response'])
        with self.assertRaises(ValueError):PolicyModel(bytes(32),k['controller'],k['response'])
    def test_role_key_reader_accepts_only_requested_role(self):
        from siliconsign.__main__ import read_role_key
        with tempfile.TemporaryDirectory() as path:
            p=Path(path)/'controller.json'
            p.write_text(json.dumps({'controller':DEMO_KEYS['controller'].hex()}));p.chmod(0o600)
            self.assertEqual(read_role_key(p,'controller'),DEMO_KEYS['controller'])
            with self.assertRaises(ValueError):read_role_key(p,'admin')
            p.write_text(json.dumps({'controller':bytes(32).hex()}))
            with self.assertRaises(ValueError):read_role_key(p,'controller')
            p.write_text(json.dumps({'controller':DEMO_KEYS['controller'].hex()}));p.chmod(0o644)
            with self.assertRaises(ValueError):read_role_key(p,'controller')

    def test_evidence_includes_sources_and_binds_account(self):
        f=command(Op.COMMIT_INVOICE,account=digest('account'),amount=100)
        sources={'invoice':{'lines':[{'amount_cents':100}]},'purchase_order':{},'receipt':{}}
        p,h=make_packet(f,sources,simulated=True)
        self.assertEqual(p['source_documents'],sources)
        self.assertTrue(p['simulated'])
        self.assertEqual(h,digest(canonical_bytes(p)))
        self.assertNotEqual(h,make_packet(f.replace(account=digest('other')),sources,simulated=True)[1])
        self.assertEqual(len(vetting_missing({})),4)

class PolicyTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.f=Fixture(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def test_status_is_read_only_even_locked_or_wrong_session(self):
        self.assertEqual(self.f.raw(command(Op.STATUS,session=bytes(16))),Status.OK)
        self.f.model.reset()
        response,_=self.f.model.process(command(Op.STATUS))
        self.assertEqual(Frame.unpack(response).status,Status.OK)
        self.assertEqual(Frame.unpack(response).integer(58,2),0)
        self.assertEqual(Frame.unpack(response).words[63],0)
    def test_clock_overflow_latches_lock_instead_of_saturating_authority(self):
        self.f.model.reset()
        init=command(Op.INIT,seq=1,session=bytes(range(16)),timestamp=MAX_U64,
                     daily_cap=1000,threshold=100,cooldown=1,max_count=1)
        self.assertEqual(self.f.raw(init,'admin'),Status.OK)
        self.f.model.advance(1)
        self.assertEqual(self.f.raw(command(Op.PAY,seq=1,session=bytes(range(16)))),Status.LOCKED)
        self.assertTrue(self.f.model.locked)
        self.assertFalse(self.f.model.initialized)
        self.assertEqual(self.f.model.now,MAX_U64)

    def test_initialization_currency_and_count_validation(self):
        self.f.model.reset()
        base=command(Op.INIT,seq=1,session=bytes(range(16)),timestamp=NOW,daily_cap=1000,
                     threshold=100,cooldown=1,max_count=1)
        for changes,expected in [({'currency':0},Status.CURRENCY),({'currency':1000},Status.CURRENCY),
                                  ({'max_count':65536},Status.RANGE)]:
            self.assertEqual(self.f.raw(base.replace(**changes),'admin'),expected)

    def test_valid_payment_and_duplicates(self):
        inv=self.f.commit();status,_,_=self.f.pay(inv);self.assertEqual(status,Status.OK)
        self.assertEqual(self.f.pay(inv)[0],Status.PAID)
    def test_account_amount_currency_flags_bind(self):
        inv=self.f.commit()
        for changes,status in [({'account':digest('attacker')},Status.ACCOUNT),({'amount':9999},Status.AMOUNT),
                              ({'currency':978},Status.CURRENCY),({'flags':1},Status.FLAGS),({'vendor_version':2},Status.VERSION)]:
            with self.subTest(changes=changes):self.assertEqual(self.f.pay(inv.replace(**changes))[0],status)
        self.assertEqual(self.f.model.spent,0)
    def test_wrong_role_and_tampered_management(self):
        c=command(Op.CHANGE_BANK,session=self.f.c.session,seq=99,vendor=0,vendor_version=2,
                  account=digest('attacker'),evidence=digest('fake'))
        self.assertEqual(self.f.raw(c),Status.AUTH)
        self.assertEqual(self.f.raw(c,'controller'),Status.AUTH)
        self.assertEqual(self.f.model.vendors[0].version,1)
    def test_split_payment_cumulative_threshold(self):
        for i in range(2):self.assertEqual(self.f.pay(self.f.commit(i,20_000))[0],Status.OK)
        third=self.f.commit(2,20_000)
        self.assertEqual(self.f.pay(third)[0],Status.APPROVAL)
        self.f.c.manage(third.replace(op=Op.APPROVE_EXCEPTION,ttl=10))
        self.assertEqual(self.f.pay(third)[0],Status.OK)
    def test_exception_expiry_and_no_override_caps(self):
        inv=self.f.commit(amount=60_000,flags=1)
        self.f.c.manage(inv.replace(op=Op.APPROVE_EXCEPTION,ttl=1));self.f.model.advance(1)
        self.assertEqual(self.f.pay(inv)[0],Status.APPROVAL)
        self.f.c.manage(inv.replace(op=Op.APPROVE_EXCEPTION,ttl=2));self.assertEqual(self.f.pay(inv)[0],Status.OK)
        extra=self.f.commit(1,30_000);self.f.c.manage(extra.replace(op=Op.APPROVE_EXCEPTION,ttl=2))
        self.assertEqual(self.f.pay(extra)[0],Status.VENDOR_CAP)
    def test_bank_change_rebind_and_hold(self):
        old=self.f.commit();new=digest('new verified account')
        self.f.c.manage(command(Op.CHANGE_BANK,vendor=0,account=new,vendor_version=2,evidence=digest('new evidence')))
        self.assertEqual(self.f.pay(old)[0],Status.ACCOUNT)
        rebound=self.f.c.manage(old.replace(op=Op.REBIND_INVOICE,account=new,vendor_version=2,evidence=digest('rebind')))[0]
        self.assertEqual(self.f.pay(rebound)[0],Status.COOLDOWN)
        self.f.model.advance(60)
        self.assertEqual(self.f.pay(rebound)[0],Status.OK)
        self.assertEqual(len(self.f.bank.entries()),4)
    def test_unpaid_rebind_preserves_amount(self):
        old=self.f.commit();new=digest('new account')
        self.f.c.manage(command(Op.CHANGE_BANK,vendor=0,account=new,vendor_version=2,evidence=digest('new evidence')))
        f=old.replace(op=Op.REBIND_INVOICE,session=self.f.c.session,seq=99,account=new,vendor_version=2,amount=999)
        self.assertEqual(self.f.raw(f,'controller'),Status.AMOUNT)
    def test_duplicate_invoice_identity(self):
        first=self.f.commit()
        duplicate=first.replace(invoice=1,seq=99)
        self.assertEqual(self.f.raw(duplicate,'controller'),Status.DUPLICATE)
    def test_day_cannot_be_advanced_early(self):
        inv=self.f.commit();self.f.pay(inv)
        early=command(Op.ADVANCE_DAY,session=self.f.c.session,seq=99,timestamp=NOW+86400)
        self.assertEqual(self.f.raw(early,'admin'),Status.TIME)
        self.f.model.advance(86400)
        new=self.f.commit(1)
        self.assertEqual(self.f.pay(new)[0],Status.DAY_ROLLOVER)
        self.f.c.manage(command(Op.ADVANCE_DAY,timestamp=NOW+86400))
        self.assertEqual(self.f.pay(new)[0],Status.OK)
        self.assertTrue(self.f.model.invoices[0].paid)
    def test_count_cap_and_disabled_vendor(self):
        for i in range(3):self.assertEqual(self.f.pay(self.f.commit(i,100))[0],Status.OK)
        fourth=self.f.commit(3,100)
        self.assertEqual(self.f.pay(fourth)[0],Status.COUNT)
        self.f.c.manage(command(Op.DISABLE_VENDOR,vendor=0))
        self.assertEqual(self.f.pay(fourth)[0],Status.DISABLED)
    def test_sequence_and_session(self):
        inv=self.f.commit()
        wrong=inv.replace(op=Op.PAY,seq=1,session=bytes(range(16)))
        self.assertEqual(self.f.raw(wrong),Status.SESSION)
        zero=inv.replace(op=Op.PAY,seq=0)
        self.assertEqual(self.f.raw(zero),Status.SEQUENCE)
    def test_lock_latches_until_reset(self):
        self.f.c.manage(command(Op.LOCK))
        inv=command(Op.INIT,session=self.f.c.session,seq=99,timestamp=NOW,daily_cap=1000,threshold=100,cooldown=1,max_count=3)
        self.assertEqual(self.f.raw(inv,'admin'),Status.LOCKED)
    def test_oversized_and_noncanonical_fields(self):
        inv=self.f.commit();w=list(inv.words);w[56]=1
        self.assertEqual(self.f.raw(Frame(tuple(w)),'controller'),Status.BAD_FRAME)
        inv=inv.replace(op=Op.PAY,seq=1,invoice=64)
        self.assertEqual(self.f.raw(inv),Status.RANGE)

class RecoveryTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.f=Fixture(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def test_signed_but_unsettled_authorization_stays_reserved(self):
        inv=self.f.commit(amount=30_000)
        pay=inv.replace(op=Op.PAY,seq=1)
        response,mac=self.f.device.transact(pay)  # authentic reservation, bank not called
        self.assertEqual(Frame.unpack(response).status,Status.OK)
        self.assertEqual(len(self.f.bank.snapshot()['receipts']),0)
        self.f.model.reset();self.f.c.recover(timestamp=NOW)
        self.assertEqual(self.f.model.spent,30_000)
        self.assertEqual(self.f.model.vendors[0].spent,30_000)
        self.assertNotIn(0,self.f.model.invoices)
        with self.assertRaises(GatewayError):self.f.bank.settle(pay.pack(),response,mac)
    def test_recovery_preserves_paid_and_unpaid_and_expires_approval(self):
        first=self.f.commit();self.f.pay(first)
        unpaid=self.f.commit(1,20_000,flags=1)
        self.f.c.manage(unpaid.replace(op=Op.APPROVE_EXCEPTION,ttl=100))
        old=self.f.c.session;self.f.model.reset();self.f.c.recover(timestamp=NOW)
        self.assertNotEqual(old,self.f.c.session)
        self.assertEqual(self.f.model.spent,10_000)
        self.assertNotIn(0,self.f.model.invoices)
        self.assertIn(1,self.f.model.invoices)
        self.assertEqual(self.f.pay(unpaid)[0],Status.APPROVAL)
        self.assertEqual(len(self.f.bank.entries()),6)
    def test_pending_intent_stops_recovery_and_revokes_session(self):
        inv=self.f.commit();p=inv.replace(op=Op.PAY,seq=1)
        self.f.journal.begin(p.pack(),bytes(32));self.f.model.reset()
        with self.assertRaises(RecoveryRequired):self.f.c.recover(timestamp=NOW)
        self.assertTrue(self.f.device.locked);self.assertIsNone(self.f.bank.active_session)
    def test_timeout_after_hardware_approval_is_not_released(self):
        inv=self.f.commit();p=inv.replace(op=Op.PAY,seq=1)
        class DropResponse:
            def exchange(inner,frame,mac):
                self.f.model.process(frame,mac)
                raise TransportError('connection lost after device authorization')
            def acknowledge(inner):pass
        self.f.device.transport=DropResponse()
        with self.assertRaises(TransportError):self.f.device.transact(p)
        self.assertEqual(self.f.model.spent,10_000)
        self.assertTrue(self.f.device.locked);self.assertTrue(self.f.journal.pending())
    def test_failure_before_send_prevents_hardware_mutation(self):
        inv=self.f.commit();p=inv.replace(op=Op.PAY,seq=1)
        original=self.f.journal.begin
        def diskfail(*args):raise OSError('disk full')
        self.f.journal.begin=diskfail
        with self.assertRaises(OSError):self.f.device.transact(p)
        self.assertEqual(self.f.model.spent,0)
        self.assertTrue(self.f.device.locked)
        self.f.journal.begin=original
    def test_recovery_restarts_bank_hold_not_skips_it(self):
        old=self.f.commit();new=digest('replacement')
        self.f.c.manage(command(Op.CHANGE_BANK,vendor=0,account=new,vendor_version=2,evidence=digest('verified')))
        rebound=self.f.c.manage(old.replace(op=Op.REBIND_INVOICE,account=new,vendor_version=2,evidence=digest('review')))[0]
        self.f.model.advance(59);self.f.model.reset();self.f.c.recover(timestamp=NOW+59)
        self.assertEqual(self.f.model.vendors[0].hold_until,NOW+119)
        self.assertEqual(self.f.pay(rebound)[0],Status.COOLDOWN)
    def test_recovery_does_not_reopen_operator_lock(self):
        self.f.c.manage(command(Op.LOCK));self.f.model.reset()
        with self.assertRaises(RecoveryRequired):self.f.c.recover(timestamp=NOW)
    def test_crash_after_recovery_init_never_uses_partial_snapshot(self):
        self.f.commit()
        self.f.c.manage(command(Op.ADD_VENDOR,vendor=1,account=digest('vendor2'),vendor_version=1,
            vendor_cap=10000,evidence=digest('vendor2 evidence')))
        self.f.model.reset()
        original=self.f.c._send
        def crash_after_init(frame):
            result=original(frame)
            if frame.op==Op.INIT:raise RuntimeError('power loss after new INIT')
            return result
        self.f.c._send=crash_after_init
        with self.assertRaises(RuntimeError):self.f.c.recover(timestamp=NOW)
        self.f.model.reset();self.f.c._send=original
        with self.assertRaises(RecoveryRequired):self.f.c.snapshot()
        with self.assertRaises(RecoveryRequired):self.f.c.recover(timestamp=NOW)
        self.assertTrue(self.f.device.locked);self.assertIsNone(self.f.bank.active_session)
        self.assertFalse(self.f.model.initialized)

    def test_crash_during_vendor_restore_cannot_unlock_partial_generation(self):
        inv=self.f.commit();self.f.pay(inv)
        self.f.c.manage(command(Op.ADD_VENDOR,vendor=1,account=digest('second'),vendor_version=1,
            vendor_cap=10000,evidence=digest('second evidence')))
        self.f.model.reset();original=self.f.c._send
        def crash_after_first_vendor(frame):
            result=original(frame)
            if frame.op==Op.ADD_VENDOR and frame.words[8]==0:raise RuntimeError('power loss midway')
            return result
        self.f.c._send=crash_after_first_vendor
        with self.assertRaises(RuntimeError):self.f.c.recover(timestamp=NOW)
        self.assertEqual(self.f.model.spent,10000)
        self.f.model.reset();self.f.c._send=original
        with self.assertRaises(RecoveryRequired):self.f.c.recover(timestamp=NOW)
        self.assertTrue(self.f.device.locked)

    def test_unsigned_management_received_by_bridge_rejected_by_hardware(self):
        f=command(Op.CHANGE_BANK,session=self.f.c.session,seq=99,vendor=0,account=digest('attacker'),vendor_version=2,evidence=digest('fake'))
        response,_=submit_envelope(self.f.c,f,bytes(32))
        self.assertEqual(Frame.unpack(response).status,Status.AUTH)
        self.assertEqual(self.f.model.vendors[0].account,self.f.account)

class DemoTests(unittest.TestCase):
    def test_all_scenarios_and_balanced_books(self):
        with tempfile.TemporaryDirectory() as path:
            report=run_demo(path)
            self.assertEqual(report['passed'],report['total'])
            self.assertGreaterEqual(report['total'],13)
            self.assertTrue(report['books_balanced'])
            self.assertEqual(report['settled_payment_count'],3)
            self.assertEqual(report['reserved_cents_after_recovery'],90_000)

if __name__=='__main__':unittest.main()
