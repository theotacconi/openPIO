#!/usr/bin/env python3
"""SWD host against a reference ADIv5 target: DP / AP reads and writes, ACK, parity, no
contention on SWDIO around the turnarounds."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
sys.path.insert(0, str(HERE.parents[1] / "sim" / "model"))
from bench import Bench                                  # noqa: E402
from chip import PIN_MASK                                # noqa: E402
from devices import SwdTarget                            # noqa: E402

CLK, DIO = 0, 1
DPIDR = 0x0BC11477


def request(ap, rnw, addr):
    a2, a3 = addr >> 2 & 1, addr >> 3 & 1
    bits = [1, ap, rnw, a2, a3, (ap + rnw + a2 + a3) % 2, 0, 1]
    return sum(b << i for i, b in enumerate(bits))


def write(ap, addr, value):
    return [request(ap, 0, addr), value & 0xFFFF, value >> 16, bin(value).count("1") % 2]


class Swd(unittest.TestCase):

    def test_reads_and_writes(self):
        bench = Bench()
        bench.load(0, (HERE / "swd.asm").read_text(), sideset_base=CLK, set_base=DIO, set_count=1,
                   out_base=DIO, in_base=DIO)
        bench.chip.reg_write(0, PIN_MASK, 1 << CLK | 1 << DIO)
        target = bench.add(SwdTarget(CLK, DIO, {(0, 0x0): DPIDR}))
        bench.send(0, [request(0, 1, 0x0)] + write(1, 0x4, 0xCAFE1234) + [request(1, 1, 0x4)]
                   + write(0, 0x8, 0x00000000) + [request(0, 1, 0x8)])
        bench.start(1)
        bench.run(1500)
        self.assertEqual(bench.errors(), [])
        self.assertEqual(target.log, [("r", 0, 0x0, DPIDR), ("w", 1, 0x4, 0xCAFE1234),
                                      ("r", 1, 0x4, 0xCAFE1234), ("w", 0, 0x8, 0), ("r", 0, 0x8, 0)])
        words = bench.received[0]
        reads = [words[0:4], words[5:9], words[10:14]]   # ACK, data lo, data hi, parity
        for (ack, lo, hi, par), value in zip(reads, [DPIDR, 0xCAFE1234, 0]):
            self.assertEqual(ack >> 13, 0b001, "ACK OK")
            self.assertEqual(lo | hi << 16, value)
            self.assertEqual(par >> 15, bin(value).count("1") % 2)
        self.assertEqual([words[4] >> 13, words[9] >> 13], [1, 1], "write ACKs")


if __name__ == "__main__":
    unittest.main()
