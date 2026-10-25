"""MMIO writes use scalar words, never struct's preliminary destination clear.

No /dev/mem or board is accessed. The observed-word fixture enforces the RTL
CTRL contract while exercising the real transport exchange/ACK methods.
"""
import mmap
import struct
import sys
import unittest
from unittest.mock import patch

from siliconsign.protocol import Op, command
from siliconsign.transport import MMIOTransport, TransportError


class ObservedWords:
    def __init__(self, memory):
        self.memory = memory
        self.stores = []
        self.released = 0

    def __call__(self, memory):
        assert memory is self.memory
        return self

    def cast(self, format):
        assert format == "I"
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.released += 1

    def __setitem__(self, index, value):
        # Slices or bytes are not accepted by this hardware-contract fixture.
        assert type(index) is int and type(value) is int
        offset = index * 4
        self.stores.append((offset, value))
        if offset == MMIOTransport.CTRL:
            assert value in (1, 2), "RTL rejects a transient zero/invalid CTRL write"
            status = 6 if value == 1 else 4
            self.memory[MMIOTransport.STATUS:MMIOTransport.STATUS + 4] = status.to_bytes(4, "little")
        self.memory[offset:offset + 4] = value.to_bytes(4, "little")


class MMIOWritesTests(unittest.TestCase):
    def transport(self, memory):
        transport = MMIOTransport.__new__(MMIOTransport)
        transport.mem = memory
        transport.timeout = 1
        return transport

    def test_full_status_exchange_and_ack_emit_only_intended_scalar_stores(self):
        memory = bytearray(4096)
        memory[MMIOTransport.STATUS:MMIOTransport.STATUS + 4] = (4).to_bytes(4, "little")
        expected_response = bytes(range(256))
        expected_tag = bytes(range(32))
        memory[MMIOTransport.RESPONSE:MMIOTransport.RESPONSE + 256] = expected_response
        memory[MMIOTransport.RESPONSE_MAC:MMIOTransport.RESPONSE_MAC + 32] = expected_tag
        device = self.transport(memory)
        observed = ObservedWords(memory)
        frame, tag = command(Op.STATUS).pack(), bytes(32)
        with patch("siliconsign.transport.memoryview", observed, create=True), patch(
            "siliconsign.transport.struct.pack_into",
            side_effect=AssertionError("pack_into can clear live CTRL before START/ACK"),
        ):
            self.assertEqual(device.exchange(frame, tag), (expected_response, expected_tag))
            device.acknowledge()
        expected = [(0x100 + 4*i, value) for i, value in enumerate(struct.unpack("<64I", frame))]
        expected += [(0x200 + 4*i, 0) for i in range(8)]
        expected += [(MMIOTransport.CTRL, 1), (MMIOTransport.CTRL, 2)]
        self.assertEqual(observed.stores, expected)
        self.assertEqual(observed.released, 74)
        self.assertEqual(device._read(MMIOTransport.STATUS), 4)

    def test_native_scalar_store_preserves_neighbors_and_releases_mapping_view(self):
        with mmap.mmap(-1, 4096) as memory:
            memory[:] = b"\xa5" * 4096
            device = self.transport(memory)
            with patch("siliconsign.transport.struct.pack_into", side_effect=AssertionError):
                device._write(0, 1)
                device._write(0x100, 0xFEDCBA98)
                device._write(4092, 0xFFFFFFFF)
            self.assertEqual(memory[:4], b"\x01\0\0\0")
            self.assertEqual(memory[4:0x100], b"\xa5" * (0x100-4))
            self.assertEqual(memory[0x100:0x104], b"\x98\xba\xdc\xfe")
            self.assertEqual(memory[0x104:4092], b"\xa5" * (4092-0x104))
            self.assertEqual(memory[4092:], b"\xff" * 4)
        # Leaving the context also proves no exported view prevents mmap.close.

    def test_invalid_offsets_and_values_never_access_memory(self):
        device = self.transport(bytearray(4096))
        with patch("siliconsign.transport.memoryview", side_effect=AssertionError, create=True):
            for offset in (-4, 1, 2, 3, 4093, 4096, True, 4.0):
                with self.subTest(offset=offset), self.assertRaises(ValueError):
                    device._write(offset, 1)
            for value in (-1, 2**32, True, 1.0, "1"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    device._write(0, value)

    def test_incompatible_native_layout_never_accesses_memory(self):
        device = self.transport(bytearray(4096))
        with patch("siliconsign.transport.memoryview", side_effect=AssertionError, create=True):
            with patch.object(sys, "byteorder", "big"), self.assertRaises(TransportError):
                device._write(0, 1)
            with patch("siliconsign.transport.struct.calcsize", return_value=8), self.assertRaises(TransportError):
                device._write(0, 1)


if __name__ == "__main__":
    unittest.main()
