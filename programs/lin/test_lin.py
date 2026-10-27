#!/usr/bin/env python3
"""LIN master against a reference LIN slave on a wired-AND bus: break of 13 bits, sync 0x55,
protected ID parity, a response from the slave (data and checksum read back) and one from the
master (checksum checked by the slave)."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
from bench import Bench                                        # noqa: E402
from devices import LinSlave, WiredAndBus, lin_checksum, lin_pid  # noqa: E402

TX, RX, BIT = 0, 1, 8  # pins as in the program


class LinMaster(unittest.TestCase):

    def test_slave_and_master_responses(self):
        slave = LinSlave(BIT, publish={0x10: [0x11, 0x22, 0x33]}, subscribe={0x21: 2})
        bench = Bench(pullups=1 << TX | 1 << RX)
        bench.load(0, (HERE / "lin_master.asm").read_text())
        bench.add(WiredAndBus([TX], [RX], [slave]))
        pub, data = lin_pid(0x21), [0xA5, 0x5A]
        bench.send(0, [lin_pid(0x10) | 4 << 8,                                  # slave answers
                       pub | 3 << 8 | 1 << 15] + data + [lin_checksum(pub, data)])  # master sends
        bench.start(1)
        bench.run(2500)
        self.assertEqual(bench.errors(), [])
        got = [w >> 8 for w in bench.received[0]]
        self.assertEqual(got, [0x11, 0x22, 0x33, lin_checksum(lin_pid(0x10), [0x11, 0x22, 0x33])])
        self.assertEqual(slave.frames, [(0x10, [0x11, 0x22, 0x33], True), (0x21, data, True)])


if __name__ == "__main__":
    unittest.main()
