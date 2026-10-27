#!/usr/bin/env python3
"""SPI master (one program, 4 modes) judged by the reference SPI slave: random words both ways,
CS_N around each word, SCK idle level, MOSI setup and hold around every sampling edge. At CLKDIV 1
MISO arrives through the input synchronizer with the least margin."""
import random
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
sys.path.insert(0, str(HERE.parents[1] / "sim" / "model"))
from bench import Bench                                  # noqa: E402
from devices import SpiSlave                             # noqa: E402

MOSI, SCK, CS, MISO = 0, 1, 2, 3


class SpiMaster(unittest.TestCase):

    def test_all_modes(self):
        rng = random.Random(3)
        for div, mode in [(div, mode) for div in (1, 2) for mode in range(4)]:
            with self.subTest(clkdiv=div, mode=mode):
                sent = [rng.randrange(256) for _ in range(20)]
                reply = [rng.randrange(256) for _ in range(20)]
                bench = Bench(pullups=1 << CS)              # CS_N pulled up on the board
                bench.load(0, (HERE / "spi_master.asm").read_text(), x=mode, clkdiv_int=div)
                # the program puts MOSI out 2 strobes before a sampling edge
                slave = bench.add(SpiSlave(SCK, MOSI, MISO, mode=mode, reply=reply, cs=CS, setup=2 * div))
                bench.send(0, [b << 8 for b in sent])
                bench.start(1)
                bench.run(3000)
                self.assertEqual(slave.words, sent, "MOSI")
                self.assertEqual([w & 0xFF for w in bench.received[0]], reply, "MISO")
                self.assertEqual(bench.errors(), [])
                self.assertEqual(bench.wave(SCK)[-1], str(mode >> 1), "SCK back at CPOL")
                cs_low = [n for lv, n in bench.runs(CS) if lv == 0]
                self.assertEqual(len(cs_low), len(sent), "CS_N low once per word")


if __name__ == "__main__":
    unittest.main()
