#!/usr/bin/env python3
"""PS/2 host against a reference PS/2 keyboard: scan codes received with their parity checked
on the chip (a 1-bit CRC), a command sent with inhibit, request to send, parity and the device's
ACK, and the keyboard's 0xFA answer."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
sys.path.insert(0, str(HERE.parents[1] / "sim" / "model"))
from bench import Bench                                  # noqa: E402
from devices import Ps2Device                            # noqa: E402

DATA, CLK = 2, 3  # pins as in the program


def setup(device):
    bench = Bench(pullups=1 << DATA | 1 << CLK)
    bench.load(0, (HERE / "ps2_host.asm").read_text())
    bench.add(device)
    bench.start(1)
    return bench


def frame(word):
    return word >> 6 & 0xFF, word & 1                    # data, parity error


class Ps2Host(unittest.TestCase):

    def test_scan_codes(self):
        keys = [0x1C, 0xF0, 0x1C, 0x5A, 0x00, 0xFF]       # 'A' pressed and released, Enter, ...
        bench = setup(Ps2Device(CLK, DATA, keys))
        bench.run(len(keys) * 1200)
        self.assertEqual(bench.errors(), [])
        self.assertEqual([frame(w) for w in bench.received[0]], [(k, 0) for k in keys])

    def test_parity_error(self):
        bench = setup(Ps2Device(CLK, DATA, [0x1C, 0x32], bad_parity={1}))
        bench.run(2500)
        self.assertEqual([frame(w) for w in bench.received[0]], [(0x1C, 0), (0x32, 1)])

    def test_command_and_answer(self):
        dev = Ps2Device(CLK, DATA)
        bench = setup(dev)
        bench.send(0, [0xED])                              # set LEDs
        bench.run(3000)
        self.assertEqual(bench.errors(), [])
        self.assertEqual(dev.received, [0xED])
        ack, answer = bench.received[0]
        self.assertEqual(ack >> 15, 0, "device ACK")
        self.assertEqual(frame(answer), (0xFA, 0), "keyboard answers 0xFA")


if __name__ == "__main__":
    unittest.main()
