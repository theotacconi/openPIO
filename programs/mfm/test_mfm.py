#!/usr/bin/env python3
"""MFM disk controller against a reference drive: sectors written are decoded from the write
line by the drive (every transition on the half-cell grid, the MFM rule at every cell boundary,
address mark, data, CRC-16/CCITT); a sector the drive plays (gap, zeros, 0xA1, data, CRC) is
found by a sync hunt in software and read back with its CRC verdict, a corrupted one too; random
settings inside what the controller promises (dividers, a disk 3 % fast or slow, its pulses
wandering). A protocol written after the ISA v3 revision."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
sys.path.insert(0, str(HERE.parents[1] / "sim" / "model"))
from bench import Bench                                  # noqa: E402
from devices import MfmDisk, crc16_ccitt                 # noqa: E402
from seeds import seeded                                 # noqa: E402

WR, RD = 0, 1
CELL = 16                                                # 2H strobes, CLKDIV 1


class MfmController(unittest.TestCase):

    def test_write_sectors(self):
        rng = seeded(7)
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
                data = seeded(9).choices(range(256), k=10)
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

    def test_sweep(self):
        """Random settings inside what mfm_controller.asm promises. Reading: a divider from 1 to
        8 (fractional), a disk 3 % slow, 3 % fast and twice at random within, each pulse up to
        1/8 of a half-cell off, sectors of 1 to 40 bytes rich in 0x00, 0xFF and 0xA1; every byte
        and the CRC verdict right. Writing: a divider from 1 to 8, the drive's checks. Measured:
        pulses 3/16 off pass at 3 % fast or slow, 1/8 at 5 %."""
        rnd = seeded(26)
        for trial in range(4):
            div = rnd.randrange(1, 9) + rnd.choice([0, rnd.randrange(256)]) / 256
            off = [-0.03, 0.03, rnd.uniform(-0.03, 0.03), rnd.uniform(-0.03, 0.03)][trial]
            data = [rnd.choice([0x00, 0xFF, 0xA1, rnd.randrange(256)]) for _ in range(rnd.randrange(1, 41))]
            cell = CELL * div
            with self.subTest(trial=trial, read=True, div=div, off=f"{off:+.2%}", n=len(data)):
                bench = Bench()
                bench.load(0, (HERE / "mfm_controller.asm").read_text(), clkdiv_int=int(div),
                           clkdiv_frac=round(div % 1 * 256))
                disk = bench.add(MfmDisk(WR, RD, round(cell), play=200, sector=data, play_cell=cell * (1 + off),
                                         jitter=cell / 16, seed=rnd.randrange(1 << 30)))
                bench.send(0, [len(data) - 1])
                bench.start(1)
                bench.run(200 + len(disk.out) + round(50 * cell), until=lambda: len(bench.received[0]) == len(data) + 3)
                got = bench.received[0]
                crc = crc16_ccitt(data)
                self.assertEqual([w & 0xFF for w in got[:-1]], [*data, crc >> 8, crc & 0xFF])
                self.assertTrue(got[-1] >> 12 & 1, "the CRC verdict")
                self.assertEqual(bench.errors(), [])
            div = rnd.randrange(1, 9)
            data = [rnd.choice([0x00, 0xFF, 0xA1, rnd.randrange(256)]) for _ in range(rnd.randrange(1, 41))]
            with self.subTest(trial=trial, write=True, div=div, n=len(data)):
                bench = Bench()
                bench.load(0, (HERE / "mfm_controller.asm").read_text(), clkdiv_int=div)
                disk = bench.add(MfmDisk(WR, RD, CELL * div))
                bench.start(1)
                bench.send(0, [0x8000 | len(data) - 1, *(b << 8 for b in data)])
                bench.run(CELL * div * 8 * (len(data) + 12))
                disk.decode(len(data))
                self.assertEqual(disk.sectors, [(data, True)])
                self.assertEqual(bench.errors(), [])


if __name__ == "__main__":
    unittest.main()
