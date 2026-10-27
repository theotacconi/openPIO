#!/usr/bin/env python3
"""SPI master (one program, 4 modes) judged by the reference SPI slave: random words both ways,
CS_N around each word, SCK idle level, MOSI setup and hold around every sampling edge."""
import random
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
sys.path.insert(0, str(HERE.parents[1] / "sim" / "model"))
from bench import Bench                                  # noqa: E402
from chip import PIN_MASK, X, Y                          # noqa: E402
from devices import SpiSlave                             # noqa: E402

MOSI, SCK, CS, MISO = 0, 1, 2, 3


class SpiMaster(unittest.TestCase):

    def test_all_modes(self):
        rng = random.Random(3)
        for mode in range(4):
            with self.subTest(mode=mode):
                sent = [rng.randrange(256) for _ in range(20)]
                reply = [rng.randrange(256) for _ in range(20)]
                bench = Bench(pullups=1 << CS)              # CS_N pulled up on the board
                bench.load(0, (HERE / "spi_master.asm").read_text(), out_base=MOSI, set_base=MOSI,
                           set_count=3, sideset_base=CS, in_base=MISO, shift_in_right=False,
                           shift_out_right=False, clkdiv_cfg_int=2)
                bench.chip.reg_write(0, X, mode)
                bench.chip.reg_write(0, Y, 1 << SCK)
                bench.chip.reg_write(0, PIN_MASK, 1 << MOSI | 1 << SCK | 1 << CS)
                # the program puts MOSI out 2 strobes (4 clocks at CLKDIV 2) before a sampling edge
                slave = bench.add(SpiSlave(SCK, MOSI, MISO, mode=mode, reply=reply, cs=CS, setup=4))
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
