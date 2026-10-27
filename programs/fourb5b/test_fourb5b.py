#!/usr/bin/env python3
"""4b5b in software through a jump table, BMC on the PAU, decoded by a reference biphase mark
receiver: every nibble comes back, symbols back to back on the line."""
import random
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
sys.path.insert(0, str(HERE.parents[1] / "sim" / "model"))
from bench import Bench                                  # noqa: E402
from devices import BmcRx                                # noqa: E402

CODES = [0b11110, 0b01001, 0b10100, 0b10101, 0b01010, 0b01011, 0b01110, 0b01111,
         0b10010, 0b10011, 0b10110, 0b10111, 0b11010, 0b11011, 0b11100, 0b11101]
PIN, BIT = 0, 8


class FourB5B(unittest.TestCase):

    def test_nibbles(self):
        nibbles = list(range(16)) + [random.Random(3).randrange(16) for _ in range(30)]
        bench = Bench()
        program = bench.load(0, (HERE / "fourb5b_tx.asm").read_text())
        rx = bench.add(BmcRx(PIN, BIT))
        bench.send(0, [2 * n for n in nibbles])
        bench.start(1)
        bench.run(len(nibbles) * 5 * BIT + 100)
        self.assertEqual(len(program), 39)
        self.assertEqual(bench.errors(), [])
        self.assertEqual(len(rx.frames), 1, "one unbroken stream")
        bits = rx.frames[0]
        symbols = [sum(bits[i + k] << k for k in range(5)) for i in range(0, len(bits) - 4, 5)]
        self.assertEqual([CODES.index(s) for s in symbols], nibbles)


if __name__ == "__main__":
    unittest.main()
