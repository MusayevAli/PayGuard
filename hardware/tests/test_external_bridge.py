import json
import tempfile
import unittest
from pathlib import Path
from siliconsign.external_bridge import ExternalBridge
from siliconsign.gateway import Journal, RecoveryRequired
from siliconsign.model import PolicyModel
from siliconsign.protocol import Op, Status, Frame, command, authenticate, envelope, parse_envelope
from siliconsign.transport import ModelTransport

A, C, R = b'a'*32, b'c'*32, b'r'*32
class Device(ModelTransport):
    def capabilities(self): return {'vendors':64,'invoices':256}
    def clock_divider_hz(self): return 50_000_000

class ExternalBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.journal=Journal(Path(self.tmp.name)/'journal.sqlite',R)
        self.device=Device(PolicyModel(A,C,R,nv=64,ni=256))
    def tearDown(self): self.tmp.cleanup()
    def init(self,bridge):
        f=command(Op.INIT,seq=1,session=b's'*16,currency=978,timestamp=100000,daily_cap=1000000,threshold=50000,cooldown=60,max_count=100)
        response,tag=parse_envelope(bridge.handle(envelope(f,authenticate(A,f))))
        self.assertEqual(Frame.unpack(response).status,Status.OK)
        return f
    def test_no_implicit_init_or_settlement_and_resume(self):
        b=ExternalBridge(self.device,self.journal)
        self.assertIsNone(b.info()['session'])
        self.assertFalse(self.device.model.initialized)
        self.init(b)
        c=ExternalBridge(self.device,self.journal)
        info=json.loads(c.handle(b'{"hello":"siliconsign.v1"}\n'))
        self.assertEqual(info['next_admin_sequence'],2)
        self.assertEqual(info['capabilities'],{'vendors':64,'invoices':256})
        self.assertEqual(info['mode'],'external-gateway')
    def test_reset_cannot_silently_reinitialize(self):
        b=ExternalBridge(self.device,self.journal);self.init(b)
        self.device.model.reset()
        with self.assertRaises(RecoveryRequired): ExternalBridge(self.device,self.journal)
    def test_unknown_initialized_device_is_blocked(self):
        b=ExternalBridge(self.device,self.journal);self.init(b)
        empty=Journal(Path(self.tmp.name)/'new.sqlite',R)
        with self.assertRaises(RecoveryRequired): ExternalBridge(self.device,empty)
    def test_pending_intent_blocks_replay(self):
        self.journal.begin(command(Op.STATUS).pack(),bytes(32))
        with self.assertRaises(RecoveryRequired): ExternalBridge(self.device,self.journal)
    def test_corrupted_response_stays_pending(self):
        b=ExternalBridge(self.device,self.journal)
        original=self.device.exchange
        self.device.exchange=lambda f,m:(original(f,m)[0],bytes(32))
        with self.assertRaises(Exception): b.handle(envelope(command(Op.STATUS)))
        self.assertTrue(b.locked)
        self.assertEqual(len(self.journal.pending()),1)
