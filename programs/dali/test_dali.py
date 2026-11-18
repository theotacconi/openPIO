#!/usr/bin/env python3
"""DALI master against reference control gear on a DALI bus behind an inverting transceiver:
forward frames at 1200 baud from a 50 MHz clock (CLKDIV 2604 + 43 / 256), Manchester on the
PAU with INV; backward frames received by the PAU 5.5 ms and 10.5 ms after the forward frame
(the settling window of IEC 62386-101), no answer, and two gear answering at once; random
settings inside what the master promises (a divider, gear whose half-bits are 20 % short or
long and jittered, any time in the window). sigrok's DALI decoder reads the same forward and
backward frames on the bus."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
from bench import Bench                                  # noqa: E402
from devices import DaliBus, DaliGear                    # noqa: E402
from sigrok import dali as sigrok_dali                   # noqa: E402
from seeds import seeded                                 # noqa: E402

TX, RX = 0, 1
MS = 1200 / 500                                          # half-bits (Te) a millisecond
HALF = 8                                                 # strobes a half-bit (the program's PAU_BAUD)


def run(frames, gear, clkdiv=(3, 0)):
    """Send the forward frames; gear: (answers, settle in Te) per DaliGear."""
    te = round(HALF * (clkdiv[0] + clkdiv[1] / 256))
    gear = [DaliGear(te, answers=a, settle=round(s * te)) for a, s in gear]
    bench = Bench()
    bench.load(0, (HERE / "dali_master.asm").read_text(), clkdiv_int=clkdiv[0], clkdiv_frac=clkdiv[1])
    bench.add(DaliBus(TX, RX, gear))
    bench.send(0, frames)
    bench.start(1)
    bench.run(len(frames) * 100 * te, until=lambda: len(bench.received[0]) == len(frames))
    return bench, gear


class DaliMaster(unittest.TestCase):

    def test_1200_baud_from_50_mhz(self):
        bench, gear = run([0xFF90], [({0xFF90: 0x04}, 5.5 * MS)], clkdiv=(2604, 43))
        self.assertEqual((gear[0].frames, bench.received[0]), ([0xFF90], [0x0104]))
        self.assertEqual(bench.errors(), [])
        bench.run(1000)
        self.assertEqual(sigrok_dali(bench, RX, HALF * (2604 + 43 / 256)),
                         [("forward", 0xFF90), ("backward", 0x04)], "sigrok")

    def test_answers_and_silence(self):
        """A command (no answer), queries answered early and late in the window, a query no gear
        answers."""
        frames = [0x0105, 0x0190, 0x03A0, 0x05A1]
        bench, gear = run(frames, [({0x0190: 0xA5}, 5.5 * MS), ({0x03A0: 0x5A}, 10.5 * MS)])
        self.assertEqual(gear[0].frames, frames)
        self.assertEqual(bench.received[0], [0, 0x01A5, 0x015A, 0])
        self.assertEqual(bench.errors(), [])
        self.assertEqual(sigrok_dali(bench, RX, HALF * 3),
                         [("forward", 0x0105), ("forward", 0x0190), ("backward", 0xA5),
                          ("forward", 0x03A0), ("backward", 0x5A), ("forward", 0x05A1)], "sigrok")
        self.assertFalse(bench.chip.sms[0].irq_flags[0])

    def test_two_gear_answer_at_once(self):
        bench, gear = run([0xFF91], [({0xFF91: 0xFF}, 6 * MS), ({0xFF91: 0x00}, 6 * MS)])
        self.assertTrue(bench.chip.sms[0].irq_flags[0], "the collision is no 9-bit frame")
        self.assertEqual(bench.errors(), [])

    def test_sweep(self):
        """Random settings inside what dali_master.asm promises: a divider from 4 (fractional),
        gear whose backward half-bits are up to 20 % short or long (a receiver takes 333.3 to
        500 us, IEC 62386-101), each edge up to 1/16 half-bit off, answering at any time in the
        5.5 to 10.5 ms window; answers 0x00, 0xFF and random. Every answer read, no IRQ."""
        rnd = seeded(19)
        for trial in range(4):
            clkdiv, frac = rnd.randrange(4, 21), rnd.choice([0, rnd.randrange(256)])
            off = [-0.2, 0.2, rnd.uniform(-0.2, 0.2), rnd.uniform(-0.2, 0.2)][trial]   # the limits too
            te = HALF * (clkdiv + frac / 256)
            queries = [rnd.randrange(0x10000) | 0x0100 for _ in range(3)]
            answers = {q: rnd.choice([0x00, 0xFF, rnd.randrange(256)]) for q in queries}
            with self.subTest(trial=trial, clkdiv=clkdiv, frac=frac, off=f"{off:+.1%}"):
                gear = DaliGear(te, answers=answers, settle=round(rnd.uniform(5.6, 10.4) * MS * te),
                                answer_te=te * (1 + off), jitter=te / 16, seed=rnd.randrange(1 << 30))
                bench = Bench()
                bench.load(0, (HERE / "dali_master.asm").read_text(), clkdiv_int=clkdiv, clkdiv_frac=frac)
                bench.add(DaliBus(TX, RX, [gear]))
                bench.send(0, queries)
                bench.start(1)
                bench.run(int(len(queries) * 100 * te), until=lambda: len(bench.received[0]) == len(queries))
                self.assertEqual(bench.errors(), [])
                self.assertEqual(gear.frames, queries)
                self.assertEqual(bench.received[0], [0x0100 | answers[q] for q in queries])
                self.assertFalse(bench.chip.sms[0].irq_flags[0])


if __name__ == "__main__":
    unittest.main()
