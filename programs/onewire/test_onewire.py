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
from chip import GPIO, PIN_MASK                          # noqa: E402
from devices import OneWireDevice, crc8_maxim            # noqa: E402

DQ, RESET = 4, 0x8000
ROM = [0x28, 0xFF, 0x4C, 0x21, 0x93, 0x16, 0x04]
ROM = ROM + [crc8_maxim(ROM)]                            # family, serial, CRC8


def run(device, commands):
    bench = Bench(pullups=1 << DQ)
    bench.load(0, (HERE / "onewire.asm").read_text(), set_base=DQ, set_count=1, in_base=DQ)
    bench.chip.reg_write(0, GPIO, 1 << DQ)
    bench.chip.reg_write(0, PIN_MASK, 1 << DQ)
    if device:
        bench.add(device)
    bench.send(0, commands)
    bench.start(1)
    bench.run(1100 * sum(1 for c in commands if c == RESET) + 600 * len(commands))
    return bench, [w >> 8 for w in bench.received[0]]


class OneWire(unittest.TestCase):

    def test_read_rom(self):
        dev = OneWireDevice(DQ, replies={0x33: ROM})
        bench, got = run(dev, [RESET, 0x33] + [0xFF] * 8)
        self.assertEqual(bench.errors(), [])
        self.assertEqual(got[0] >> 7, 0, "presence")
        self.assertEqual((dev.resets, dev.written), (1, [0x33]))
        self.assertEqual(got[1:], [0x33] + ROM)
        self.assertEqual(crc8_maxim(got[2:]), 0, "CRC8 of the ROM read back")

    def test_no_device(self):
        bench, got = run(None, [RESET])
        self.assertEqual(got[0] >> 7, 1, "no presence pulse")

    def test_write_bytes(self):
        dev = OneWireDevice(DQ)
        bench, got = run(dev, [RESET, 0xCC, 0x44, 0xA5, 0x00])
        self.assertEqual(bench.errors(), [])
        self.assertEqual(dev.written, [0xCC, 0x44, 0xA5, 0x00])


if __name__ == "__main__":
    unittest.main()
