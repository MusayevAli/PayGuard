"""Actual AXI MMIO and serial transports; model is a separate explicit adapter."""
from __future__ import annotations
import mmap
import fcntl
import json
import os
import select
import struct
import termios
import time
from .protocol import envelope, parse_envelope, HELLO_REQUEST

class TransportError(RuntimeError): pass

class ModelTransport:
    is_hardware = False
    def __init__(self,model): self.model=model
    def exchange(self,frame,mac): return self.model.process(frame,mac)
    def acknowledge(self): pass

class MMIOTransport:
    """Exclusive trusted bridge access to a SiliconSign AXI4-Lite register page.

    /dev/mem requires base physical address. /dev/uioN requires offset zero.
    Never expose this object's FD, device permissions or reset to agent processes.
    DONE is acknowledged only after the bridge journal commits the signed result.
    """
    is_hardware = True
    CTRL=0x000; STATUS=0x004; ID=0x008; CAPABILITIES=0x00c
    COMMAND=0x100; COMMAND_MAC=0x200; RESPONSE=0x300; RESPONSE_MAC=0x400
    def __init__(self,device,base=0,timeout=5.0):
        if base % mmap.PAGESIZE: raise ValueError("MMIO base must be page aligned")
        if not device.startswith("/dev/"): raise ValueError("explicit /dev device required")
        self.fd=os.open(device,os.O_RDWR|os.O_SYNC)
        try:fcntl.flock(self.fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BaseException:
            os.close(self.fd)
            raise TransportError("MMIO device already owned by another bridge")
        try:self.mem=mmap.mmap(self.fd,4096,flags=mmap.MAP_SHARED,prot=mmap.PROT_READ|mmap.PROT_WRITE,offset=base)
        except BaseException:os.close(self.fd);raise
        self.timeout=timeout
        if self._read(self.ID)!=0x53494C31:
            self.close();raise TransportError("SiliconSign IP identifier not found; verify address/bitstream")
        if not self._read(self.STATUS)&4:
            self.close();raise TransportError("hardware keys are not valid; refusing requests")
    def _read(self,offset):return struct.unpack_from("<I",self.mem,offset)[0]
    def capabilities(self):
        value=self._read(self.CAPABILITIES)
        return {"vendors":value & 0xffff,"invoices":value >> 16}
    def clock_divider_hz(self):return self._read(0x010)
    def _write(self,offset,value):
        # pack_into zero-fills its destination before packing. A transient zero
        # at CTRL is an invalid AXI command, even when followed by START/ACK.
        # CPython's native memoryview scalar setter copies one unsigned int
        # without that preliminary clear. Never use a byte/slice assignment.
        import sys
        if sys.byteorder != "little" or struct.calcsize("@I") != 4:
            raise TransportError("MMIO requires native little-endian 32-bit unsigned int")
        if type(offset) is not int or offset < 0 or offset % 4 or offset > len(self.mem)-4:
            raise ValueError("MMIO write offset must be an aligned word inside the mapping")
        if type(value) is not int or not 0 <= value <= 0xffffffff:
            raise ValueError("MMIO write value must be an unsigned 32-bit integer")
        with memoryview(self.mem).cast("I") as words:
            words[offset//4] = value
    def exchange(self,frame,mac):
        if len(frame)!=256 or len(mac)!=32:raise ValueError("wrong request lengths")
        status=self._read(self.STATUS)
        if status&3:raise TransportError("hardware BUSY or unjournaled DONE; reconcile before new request")
        for i,w in enumerate(struct.unpack("<64I",frame)):self._write(self.COMMAND+4*i,w)
        for i,w in enumerate(struct.unpack("<8I",mac)):self._write(self.COMMAND_MAC+4*i,w)
        self._write(self.CTRL,1)
        deadline=time.monotonic()+self.timeout
        while not self._read(self.STATUS)&2:
            if time.monotonic()>deadline:raise TransportError("hardware timeout; request outcome is UNKNOWN, keep reserved")
            time.sleep(.0005)
        return self.pending_response()
    def pending_response(self):
        if not self._read(self.STATUS)&2:raise TransportError("no completed hardware response")
        response=struct.pack("<64I",*[self._read(self.RESPONSE+4*i) for i in range(64)])
        mac=struct.pack("<8I",*[self._read(self.RESPONSE_MAC+4*i) for i in range(8)])
        return response,mac
    def acknowledge(self):
        if self._read(self.STATUS)&1:raise TransportError("cannot acknowledge BUSY hardware")
        self._write(self.CTRL,2)
    def close(self):
        if hasattr(self,"mem"):self.mem.close()
        if hasattr(self,"fd"):os.close(self.fd)
    def __enter__(self):return self
    def __exit__(self,*exc):self.close()

class SerialTransport:
    """Host adapter for a deliberately configured JSON-line UART bridge."""
    is_hardware = True
    def __init__(self,device,timeout=5.0,baud=115200):
        if baud!=115200:raise ValueError("v1 transport uses 115200 baud")
        self.fd=os.open(device,os.O_RDWR|os.O_NOCTTY|os.O_NONBLOCK)
        try:
            fcntl.flock(self.fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            if hasattr(termios,"TIOCEXCL"):fcntl.ioctl(self.fd,termios.TIOCEXCL)
        except BaseException:
            os.close(self.fd)
            raise TransportError("serial device already owned or cannot be exclusively opened")
        self.old=termios.tcgetattr(self.fd)
        config=termios.tcgetattr(self.fd)
        config[0]=0;config[1]=0;config[2]=termios.CS8|termios.CREAD|termios.CLOCAL;config[3]=0
        config[4]=config[5]=termios.B115200
        config[6][termios.VMIN]=0;config[6][termios.VTIME]=0
        termios.tcsetattr(self.fd,termios.TCSANOW,config)
        self.timeout=timeout;self.buffer=b""
    def _exchange_line(self,data,*,deadline=None):
        end=time.monotonic()+self.timeout if deadline is None else deadline
        while data:
            _,write,_=select.select([], [self.fd], [], max(0,end-time.monotonic()))
            if not write:raise TransportError("serial write timeout; outcome unknown")
            count=os.write(self.fd,data);data=data[count:]
        while b"\n" not in self.buffer:
            readable,_,_=select.select([self.fd],[],[],max(0,end-time.monotonic()))
            if not readable:raise TransportError("serial response timeout; do not reissue payment")
            chunk=os.read(self.fd,2048)
            if not chunk:raise TransportError("serial disconnected")
            self.buffer+=chunk
            if len(self.buffer)>4096:raise TransportError("oversized serial response")
        line,self.buffer=self.buffer.split(b"\n",1)
        return line
    def exchange(self,frame,mac):
        return parse_envelope(self._exchange_line(envelope(frame,mac)))
    def info(self):
        """Read-only, unauthenticated session discovery; never a payment authorization."""
        line=self._exchange_line((json.dumps(HELLO_REQUEST,separators=(",",":"))+"\n").encode())
        return self._parse_info(line)
    def resynchronize_read_only(self):
        """Barrier after a proven read-only STATUS failure, never after a payment.

        A newline terminates a possibly partial old request. Reading through the
        ordered HELLO reply consumes delayed STATUS output before a new command
        can be sent. The signer must still authenticate a fresh FPGA STATUS.
        """
        request=b"\n"+(json.dumps(HELLO_REQUEST,separators=(",",":"))+"\n").encode()
        deadline=time.monotonic()+self.timeout
        line=self._exchange_line(request,deadline=deadline)
        for attempt in range(8):
            try:
                return self._parse_info(line)
            except (ValueError,TransportError):
                if attempt==7:
                    raise TransportError("cannot synchronize read-only status stream") from None
                line=self._exchange_line(b"",deadline=deadline)
        raise TransportError("cannot synchronize read-only status stream")
    @staticmethod
    def _parse_info(line):
        obj=json.loads(line)
        expected={"event","informational","authorization","mode","session","locked",
                  "next_admin_sequence","next_controller_sequence","next_payment_sequence"}
        if not isinstance(obj,dict) or not expected.issubset(obj) or set(obj)-expected-{"capabilities","clock_divider_hz"} or obj.get("event")!="siliconsign.info.v1" or obj.get("informational") is not True or obj.get("authorization") is not False:
            raise TransportError("invalid hello response; not a SiliconSign informational manifest")
        if not isinstance(obj["mode"],str) or type(obj["locked"]) is not bool:
            raise TransportError("invalid hello metadata")
        if obj["session"] is not None:
            if not isinstance(obj["session"],str) or len(bytes.fromhex(obj["session"]))!=16:
                raise TransportError("invalid session identifier in hello")
        for name in ("next_admin_sequence","next_controller_sequence","next_payment_sequence"):
            value=obj[name]
            if value is not None and (type(value) is not int or not 1<=value<=0xffffffff):
                raise TransportError("invalid sequence in hello")
        return obj
    def acknowledge(self):pass  # Trusted server journals and ACKs before serial response.
    def close(self):
        try:
            termios.tcsetattr(self.fd,termios.TCSANOW,self.old)
            if hasattr(termios,"TIOCNXCL"):fcntl.ioctl(self.fd,termios.TIOCNXCL)
        finally:os.close(self.fd)
