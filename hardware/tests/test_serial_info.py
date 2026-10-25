"""USB-only discovery tests, including a real PTY with the CLI serial transport."""
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from siliconsign.__main__ import bridge_info, handle_bridge_line
from siliconsign.transport import SerialTransport, TransportError
from siliconsign.protocol import HELLO_REQUEST, Frame, Op, Status, envelope, parse_envelope
from test_software import Fixture

class SerialInfoTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.f=Fixture(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def test_hello_is_informational_and_has_no_side_effect_or_keys(self):
        count=len(self.f.journal.records())
        response=handle_bridge_line(self.f.c,b'{"hello":"siliconsign.v1"}\n')
        obj=json.loads(response)
        self.assertTrue(obj['informational']);self.assertFalse(obj['authorization'])
        self.assertEqual(obj['session'],self.f.c.session.hex())
        self.assertEqual(obj['next_admin_sequence'],self.f.c.admin_seq+1)
        self.assertEqual(obj['next_controller_sequence'],self.f.c.controller_seq+1)
        self.assertEqual(obj['next_payment_sequence'],1)
        self.assertEqual(len(self.f.journal.records()),count)
        self.assertEqual(self.f.model.spent,0)
        self.assertEqual(len(self.f.bank.entries()),0)
        for key in self.f.k.values():self.assertNotIn(key.hex(),response.decode())
        self.assertNotIn('mac',obj);self.assertNotIn('frame',obj)
    def test_extra_fields_and_wrong_hello_version_do_not_match(self):
        before=len(self.f.journal.records())
        for line in [b'{"hello":"siliconsign.v1","pay":true}\n',
                     b'{"hello":"siliconsign.v2"}\n',
                     b'{"hello":"siliconsign.v1","frame":"00","mac":"00"}\n']:
            with self.subTest(line=line):
                with self.assertRaises(ValueError):handle_bridge_line(self.f.c,line)
        self.assertEqual(len(self.f.journal.records()),before)
    def test_standard_envelope_unchanged_and_next_sequence_updates(self):
        inv=self.f.commit()
        pay=inv.replace(op=Op.PAY,seq=7,session=self.f.c.session)
        response,mac=parse_envelope(handle_bridge_line(self.f.c,envelope(pay)))
        self.assertEqual(Frame.unpack(response).status,Status.OK)
        self.assertEqual(len(mac),32)
        info=bridge_info(self.f.c)
        self.assertEqual(info['next_payment_sequence'],8)
        self.assertEqual(len(self.f.bank.snapshot()['receipts']),1)
    @unittest.skipUnless(os.name=='posix','PTY ownership test requires POSIX')
    def test_serial_port_is_exclusive_until_closed(self):
        master,slave=pty.openpty();device=os.ttyname(slave)
        first=SerialTransport(device)
        try:
            with self.assertRaises((OSError,TransportError)): SerialTransport(device)
        finally:first.close()
        second=SerialTransport(device)
        second.close()
        os.close(master);os.close(slave)
    @unittest.skipUnless(os.name=='posix','PTY serial test requires POSIX')
    def test_serial_info_cli_over_real_pty(self):
        master,slave=pty.openpty();device=os.ttyname(slave)
        failures=[];observed=[]
        def board_endpoint():
            try:
                data=b'';deadline=time.monotonic()+8
                while b'\n' not in data:
                    ready,_,_=select.select([master],[],[],max(0,deadline-time.monotonic()))
                    if not ready:raise TimeoutError('no hello received on PTY')
                    data+=os.read(master,2048)
                line=data.split(b'\n',1)[0]
                observed.append(json.loads(line))
                response=handle_bridge_line(self.f.c,line)
                while response:
                    written=os.write(master,response);response=response[written:]
            except BaseException as exc:failures.append(exc)
        thread=threading.Thread(target=board_endpoint,daemon=True);thread.start()
        env=dict(os.environ)
        env['PYTHONPATH']=str(Path(__file__).resolve().parents[1]/'python')
        try:
            result=subprocess.run([sys.executable,'-m','siliconsign','serial-info','--device',device],
                                  env=env,text=True,capture_output=True,timeout=8)
            thread.join(timeout=8)
            self.assertFalse(thread.is_alive())
            self.assertFalse(failures,failures)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(observed,[HELLO_REQUEST])
            info=json.loads(result.stdout)
            self.assertEqual(info['session'],self.f.c.session.hex())
            self.assertTrue(info['informational']);self.assertFalse(info['authorization'])
            for key in self.f.k.values():self.assertNotIn(key.hex(),result.stdout+result.stderr)
        finally:
            os.close(master);os.close(slave)

if __name__=='__main__':unittest.main()
