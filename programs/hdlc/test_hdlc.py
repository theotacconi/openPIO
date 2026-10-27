#!/usr/bin/env python3
"""HDLC transmitter judged by the reference HDLC receiver: NRZI, flags, zero insertion after
five 1s (also inside the FCS), FCS against CRC-16/X.25, frames of 1 to 20 bytes, including
bytes that look like flags."""
import random
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
sys.path.insert(0, str(HERE.parents[1] / "sim" / "model"))
from bench import Bench                                  # noqa: E402
from devices import HdlcRx                               # noqa: E402

LINE, BIT = 0, 8                                         # pin as in the program


class Hdlc(unittest.TestCase):

    def test_frames(self):
        rng = random.Random(8)
        frames = [[0x7E, 0xFF, 0xFF, 0x7E], [0x00], [0xFF] * 3] + \
                 [[rng.randrange(256) for _ in range(rng.randrange(1, 21))] for _ in range(4)]
        bench = Bench()
        bench.load(0, (HERE / "hdlc_tx.asm").read_text())
        rx = bench.add(HdlcRx(LINE, BIT))
        for f in frames:
            bench.send(0, [len(f)] + f)
        bench.start(1)
        bench.run(sum(len(f) + 4 for f in frames) * 8 * BIT * 5 // 4 + 200)
        self.assertEqual(bench.errors(), [])
        self.assertEqual([f["data"] for f in rx.frames], frames)
        self.assertTrue(all(f["fcs_ok"] for f in rx.frames), "FCS")


if __name__ == "__main__":
    unittest.main()
