#!/usr/bin/env python3
"""SENT (SAE J2716) receiver fed by the reference SENT sensor: fast-channel frames of a status
nibble, six data nibbles and the CRC4, back to back, the sensor's tick nominal (3 us), 20 %
fast or slow, with low times of 5 and 4 ticks; every nibble decoded on the chip, the CRC
checked on the chip (good frames and frames with a wrong CRC). A protocol written after the
ISA was frozen (2026-10-28), without changing the hardware."""
import random
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
sys.path.insert(0, str(HERE.parents[1] / "sim" / "model"))
from bench import Bench                                  # noqa: E402
from devices import SentTx, sent_crc4                    # noqa: E402

LINE = 0                                                 # pin as in the program
TICK = 150                                               # 3 us at 50 MHz
CRC_OK = 1 << 4                                          # on the CRC nibble's word


def frames_from(words: list[int]) -> list[tuple[int, int, list[int], int, bool]]:
    """(8 S, status, data, crc, CRC good) for every sync word (> 31) followed by 8 nibbles."""
    out = []
    for i, w in enumerate(words):
        nib = words[i + 1:i + 9]
        if w > 31 and len(nib) == 8 and all(n < 32 for n in nib):
            out.append((w, nib[0] & 0xF, [n & 0xF for n in nib[1:7]], nib[7] & 0xF,
                        bool(nib[7] & CRC_OK)))
    return out


class Sent(unittest.TestCase):

    def run_sensor(self, frames, scale=1.0, low=5):
        bench = Bench(pullups=1 << LINE)
        bench.load(0, (HERE / "sent_rx.asm").read_text())
        sensor = bench.add(SentTx(LINE, frames, TICK * scale, start=2000, low=low))
        bench.start(1)
        bench.run(sensor.end + 2000)
        self.assertEqual(bench.errors(), [])
        return frames_from(bench.received[0])

    def test_frames(self):
        rng = random.Random(11)
        for scale, low in ((1.0, 5), (0.8, 5), (1.2, 5), (1.0, 4), (0.8, 4), (1.2, 4)):
            with self.subTest(scale=scale, low=low):
                frames = [(rng.randrange(16), [rng.randrange(16) for _ in range(6)])
                          for _ in range(6)]
                got = self.run_sensor(frames, scale, low)
                self.assertEqual([(s, d, c, ok) for _, s, d, c, ok in got],
                                 [(s, d, sent_crc4(d), True) for s, d in frames])
                for sync, *_ in got:                     # 8 x the sync in strobes (15 clocks)
                    self.assertAlmostEqual(sync / 8, 56 * TICK * scale / 15, delta=2)

    def test_every_value_and_a_wrong_crc(self):
        frames = [(v, [v, 15 - v, v, 15 - v, v, 15 - v]) for v in range(16)]
        bad = [3, 1, 4, 1, 5, 9]
        frames[5] = (2, bad, sent_crc4(bad) ^ 0b0100)
        got = self.run_sensor(frames, 1.13)
        self.assertEqual(len(got), len(frames))
        for (_, s, d, c, ok), frame in zip(got, frames):
            self.assertEqual((s, d, c), (frame[0], frame[1], frame[2] if len(frame) > 2
                                         else sent_crc4(frame[1])))
            self.assertEqual(ok, len(frame) == 2, f"CRC verdict of {frame}")


if __name__ == "__main__":
    unittest.main()
