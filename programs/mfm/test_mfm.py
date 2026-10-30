#!/usr/bin/env python3
"""MFM disk controller against a reference drive: sectors written are decoded from the write
line by the drive (every transition on the half-cell grid, the MFM rule at every cell boundary,
address mark, data, CRC-16/CCITT); a sector the drive plays (gap, zeros, 0xA1, data, CRC) is
found by the sync hunt and read back with its CRC verdict, a corrupted one too. Two pages: the
writer in page 1, reached and left by JMP FAR. A protocol written after the ISA v3 revision."""
import random
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
sys.path.insert(0, str(HERE.parents[1] / "sim" / "model"))
from bench import Bench                                  # noqa: E402
from devices import MfmDisk, crc16_ccitt                 # noqa: E402

WR, RD = 0, 1
CELL = 8                                                 # 2H strobes, CLKDIV 1


class MfmController(unittest.TestCase):

    def test_write_sectors(self):
        rng = random.Random(7)
        sectors = [[rng.randrange(256) for _ in range(n)] for n in (1, 6, 16)]
        bench = Bench()
        bench.load(0, (HERE / "mfm_controller.asm").read_text())
        disk = bench.add(MfmDisk(WR, RD, CELL))
        bench.start(1)
        for data in sectors:
            bench.send(0, [0x8000 | len(data) - 1, *(b << 8 for b in data)])
            bench.run(CELL * 8 * (len(data) + 12))
            disk.decode(len(data))
            disk.edges.clear()
        self.assertEqual(disk.sectors, [(d, True) for d in sectors])
        self.assertEqual(bench.errors(), [])

    def test_read_sectors(self):
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt):
                data = [random.Random(9).randrange(256) for _ in range(10)]
                bench = Bench()
                bench.load(0, (HERE / "mfm_controller.asm").read_text())
                disk = bench.add(MfmDisk(WR, RD, CELL, play=200, sector=data))
                if corrupt:                              # a bit of the data flipped on the disk
                    k = len(disk.out) // 2
                    disk.out[k:] = [1 - v for v in disk.out[k:]]
                bench.send(0, [len(data) - 1])
                bench.start(1)
                bench.run(200 + len(disk.out) + 400, until=lambda: len(bench.received[0]) == len(data) + 3)
                got = bench.received[0]
                crc = crc16_ccitt(data)
                if not corrupt:
                    self.assertEqual([w & 0xFF for w in got[:-1]], [*data, crc >> 8, crc & 0xFF])
                self.assertEqual(bool(got[-1] >> 12 & 1), not corrupt, "the CRC verdict")
                self.assertEqual(bench.errors(), [])


if __name__ == "__main__":
    unittest.main()
