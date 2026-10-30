#!/usr/bin/env python3
"""SPI target through the PAU's external bit clock, against the reference SPI master: transfers
of several bytes both ways, answers from the TX FIFO and 0xFF when it is empty, MISO set up
before every rising SCK edge (checked by the master), a byte cut by CS_N dropped. A program
written after the ISA v3 revision."""
import random
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
sys.path.insert(0, str(HERE.parents[1] / "sim" / "model"))
from bench import Bench                                  # noqa: E402
from devices import SpiMaster                            # noqa: E402

MOSI, SCK, CS, MISO = 0, 1, 2, 3


class SpiPauTarget(unittest.TestCase):

    def run_target(self, transfers, answers):
        bench = Bench(pullups=1 << MISO | 1 << CS)
        bench.load(0, (HERE / "spi_pau_target.asm").read_text())
        master = bench.add(SpiMaster(SCK, MOSI, MISO, CS, transfers, half=14, start=50, lead=14,
                                     gap=40))
        bench.send(0, [a << 8 for a in answers])
        bench.start(1)
        bench.run(master.end + 50)
        return bench, master

    def test_transfers(self):
        rng = random.Random(6)
        transfers = [[rng.randrange(256) for _ in range(n)] for n in (1, 4, 9)]
        answers = [rng.randrange(256) for _ in range(12)]
        bench, master = self.run_target(transfers, answers)
        self.assertEqual(bench.errors(), [])
        self.assertEqual([w & 0xFF for w in bench.received[0]], sum(transfers, []), "MOSI")
        self.assertEqual(sum(master.received, []), answers + [0xFF] * 2, "MISO; 0xFF once out")

    def test_a_byte_cut_by_cs(self):
        bench, master = self.run_target([[0x12, (0xA0, 5)], [0x34]], [0x55, 0x66, 0x77])
        self.assertEqual([w & 0xFF for w in bench.received[0]], [0x12, 0x34], "the cut byte dropped")
        self.assertEqual(master.received, [[0x55], [0x77]], "its answer used up")
        self.assertEqual(bench.errors(), [])


if __name__ == "__main__":
    unittest.main()
