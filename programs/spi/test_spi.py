#!/usr/bin/env python3
"""SPI master (one program, 4 modes) judged by the reference SPI slave: random words both ways,
CS_N around each word, SCK idle level, MOSI setup and hold around every sampling edge. At CLKDIV 1
MISO arrives through the input synchronizer with the least margin. SPI target driven by the
reference SPI master at clk / 28: random words both ways in transfers of several words, the
answer 0xFF when the host has none, MISO released while CS_N is high, a word cut by CS_N rising
dropped, IRQ 0 when the RX FIFO overflows, 12-bit words by the push threshold alone."""
import random
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
sys.path.insert(0, str(HERE.parents[1] / "sim" / "model"))
from bench import Bench                                  # noqa: E402
from devices import SpiMaster, SpiSlave                  # noqa: E402

MOSI, SCK, CS, MISO = 0, 1, 2, 3


class SpiMasterTest(unittest.TestCase):

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


class SpiTargetTest(unittest.TestCase):
    SCK, MOSI, CS, MISO = 0, 1, 2, 3

    def run_target(self, transfers, answers, host_word_clocks=0, bits=8):
        bench = Bench(pullups=1 << self.MISO, host_word_clocks=host_word_clocks)
        bench.load(0, (HERE / "spi_target.asm").read_text(), push_thresh=bits & 15)
        master = bench.add(SpiMaster(self.SCK, self.MOSI, self.MISO, self.CS, transfers, half=14,
                                     start=50, lead=14, gap=40, bits=bits))
        bench.send(0, [a << (16 - bits) for a in answers])
        bench.start(1)
        bench.run(master.end + 50)
        return bench, master

    def test_transfers(self):
        rng = random.Random(4)
        transfers = [[rng.randrange(256) for _ in range(n)] for n in (1, 4, 9)]
        answers = [rng.randrange(256) for _ in range(12)]
        bench, master = self.run_target(transfers, answers)
        self.assertEqual(bench.errors(), [])
        self.assertEqual([w & 0xFF for w in bench.received[0]], sum(transfers, []), "MOSI")
        self.assertEqual(sum(master.received, []), answers + [0xFF] * 2, "MISO; 0xFF once out")
        released = [bench.chip.pins[1] >> self.MISO & 1 for _ in range(1)]
        self.assertEqual(released, [0], "MISO released after the transfer")

    def test_12_bit_words(self):
        bench, master = self.run_target([[0xABC, 0x123], [0xFED]], [0x5A5, 0x0F0, 0x777], bits=12)
        self.assertEqual(bench.received[0], [0xABC, 0x123, 0xFED])
        self.assertEqual(master.received, [[0x5A5, 0x0F0], [0x777]])
        self.assertEqual(bench.errors(), [])

    def test_word_cut_by_cs(self):
        bench, master = self.run_target([[0x12, (0x34, 5)], [0x56]], [0xA1, 0xA2, 0xA3])
        self.assertEqual([w & 0xFF for w in bench.received[0]], [0x12, 0x56])
        self.assertEqual(master.received, [[0xA1], [0xA3]], "the cut word's answer is used up")

    def test_rx_fifo_overflow(self):
        bench, master = self.run_target([[k & 0xFF for k in range(140)]], [], host_word_clocks=10 ** 9)
        self.assertTrue(bench.chip.sms[0].irq_flags[0])
        self.assertEqual(bench.chip.sms[0].rx_level, 133)


if __name__ == "__main__":
    unittest.main()
