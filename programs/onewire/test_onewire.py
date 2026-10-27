#!/usr/bin/env python3
"""1-Wire master judged by a reference 1-Wire device: reset and presence, written bytes, read
bytes (a ROM with its CRC8), slot timing."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
sys.path.insert(0, str(HERE.parents[1] / "sim" / "model"))
from bench import Bench                                  # noqa: E402
from devices import OneWireDevice, crc8_maxim            # noqa: E402

DQ, RESET, CRC_RESET = 4, 0x8000, 0x4000
ROM = [0x28, 0xFF, 0x4C, 0x21, 0x93, 0x16, 0x04]
ROM = ROM + [crc8_maxim(ROM)]                            # family, serial, CRC8


def run(device, commands):
    bench = Bench(pullups=1 << DQ)
    bench.load(0, (HERE / "onewire.asm").read_text())
    if device:
        bench.add(device)
    bench.send(0, commands)
    bench.start(1)
    bench.run(1100 * sum(1 for c in commands if c == RESET) + 600 * len(commands))
    return bench, bench.received[0]


class OneWire(unittest.TestCase):

    def test_read_rom(self):
        dev = OneWireDevice(DQ, replies={0x33: ROM})
        bench, words = run(dev, [RESET, 0x33, CRC_RESET | 0xFF] + [0xFF] * 7)
        got = [w >> 8 for w in words]
        self.assertEqual(bench.errors(), [])
        self.assertEqual(got[0] >> 7, 0, "presence")
        self.assertEqual((dev.resets, dev.written), (1, [0x33]))
        self.assertEqual(got[1:], [0x33] + ROM)
        self.assertEqual([w & 1 for w in words[2:]], [0] * 7 + [1], "CRC8 right after the 8th byte")

    def test_bad_crc(self):
        dev = OneWireDevice(DQ, replies={0x33: ROM[:-1] + [ROM[-1] ^ 0x01]})
        bench, words = run(dev, [RESET, 0x33, CRC_RESET | 0xFF] + [0xFF] * 7)
        self.assertEqual(words[-1] & 1, 0, "CRC8 flags the corrupted ROM")

    def test_no_device(self):
        bench, words = run(None, [RESET])
        self.assertEqual(words[0] >> 15, 1, "no presence pulse")

    def test_write_bytes(self):
        dev = OneWireDevice(DQ)
        bench, got = run(dev, [RESET, 0xCC, 0x44, 0xA5, 0x00])
        self.assertEqual(bench.errors(), [])
        self.assertEqual(dev.written, [0xCC, 0x44, 0xA5, 0x00])


if __name__ == "__main__":
    unittest.main()
