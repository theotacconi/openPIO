#!/usr/bin/env python3
"""DALI master against reference control gear on a DALI bus behind an inverting transceiver:
forward frames at 1200 baud from a 50 MHz clock (CLKDIV 2604 + 43 / 256), Manchester on the
PAU with INV; backward frames received by the PAU 5.5 ms and 10.5 ms after the forward frame
(the settling window of IEC 62386-101), no answer, and two gear answering at once."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
from bench import Bench                                  # noqa: E402
from devices import DaliBus, DaliGear                    # noqa: E402

TX, RX = 0, 1
MS = 1200 / 500                                          # half-bits (Te) a millisecond


def run(frames, gear, clkdiv=(3, 0)):
    """Send the forward frames; gear: (answers, settle in Te) per DaliGear."""
    te = round(8 * (clkdiv[0] + clkdiv[1] / 256))
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

    def test_answers_and_silence(self):
        """A command (no answer), queries answered early and late in the window, a query no gear
        answers."""
        frames = [0x0105, 0x0190, 0x03A0, 0x05A1]
        bench, gear = run(frames, [({0x0190: 0xA5}, 5.5 * MS), ({0x03A0: 0x5A}, 10.5 * MS)])
        self.assertEqual(gear[0].frames, frames)
        self.assertEqual(bench.received[0], [0, 0x01A5, 0x015A, 0])
        self.assertEqual(bench.errors(), [])
        self.assertFalse(bench.chip.sms[0].irq_flags[0])

    def test_two_gear_answer_at_once(self):
        bench, gear = run([0xFF91], [({0xFF91: 0xFF}, 6 * MS), ({0xFF91: 0x00}, 6 * MS)])
        self.assertTrue(bench.chip.sms[0].irq_flags[0], "the collision is no 9-bit frame")
        self.assertEqual(bench.errors(), [])


if __name__ == "__main__":
    unittest.main()
