#!/usr/bin/env python3
"""10BASE-T transmitter judged by a reference Manchester receiver: preamble and SFD, frame bits,
FCS against zlib.crc32, half-bit timing, TD- the complement of TD+, TP_IDL."""
import random
import sys
import unittest
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
sys.path.insert(0, str(HERE.parents[1] / "sim" / "model"))
from bench import Bench                                  # noqa: E402
from chip import Y                                       # noqa: E402
from devices import ManchesterRx, bits_to_bytes          # noqa: E402

TDP, TDN, BIT = 0, 1, 4


class EthernetTx(unittest.TestCase):

    def test_frames(self):
        rng = random.Random(4)
        frames = [bytes(rng.randrange(256) for _ in range(n)) for n in (60, 64, 100)]
        bench = Bench()
        bench.load(0, (HERE / "eth_tx.asm").read_text(), out_base=TDP, set_base=TDP, set_count=2)
        bench.chip.reg_write(0, Y, 0x5555)
        for f in frames:
            bench.send(0, [len(f) // 2] + [f[i] | f[i + 1] << 8 for i in range(0, len(f), 2)])
        rx = bench.add(ManchesterRx(TDP, BIT))
        bench.start(1)
        bench.run(sum(len(f) + 20 for f in frames) * 8 * BIT + 200)
        self.assertEqual(bench.errors(), [])
        self.assertEqual(len(rx.frames), len(frames))
        for f, bits in zip(frames, rx.frames):
            data = bits_to_bytes(bits)
            self.assertEqual(data[:8], [0x55] * 7 + [0xD5], "preamble, SFD")
            self.assertEqual(bytes(data[8:-4]), f)
            self.assertEqual(bytes(data[-4:]), zlib.crc32(f).to_bytes(4, "little"), "FCS")
        tdp, tdn = bench.wave(TDP), bench.wave(TDN)
        self.assertTrue(all(n != p or p == "0" for p, n in zip(tdp, tdn)), "TD- = not TD+, or idle")
        self.assertIn("1" * 8 + "0", tdp, "TP_IDL: high for 2 bit times, then idle")


if __name__ == "__main__":
    unittest.main()
