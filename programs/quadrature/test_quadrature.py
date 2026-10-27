#!/usr/bin/env python3
"""Quadrature encoder counter judged against the reference encoder: random walks forward and
back at the fastest step rate the program states (8 strobes), the position read from register
X at any time; steps faster than that raise the missed-step IRQ."""
import random
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
sys.path.insert(0, str(HERE.parents[1] / "sim" / "model"))
from bench import Bench, Device                          # noqa: E402
from chip import IRQ, X                                  # noqa: E402
from devices import QuadratureEncoder                    # noqa: E402

A, B = 0, 1                                              # pins as in the program


def run(steps, period, clkdiv=1):
    bench = Bench()
    bench.load(0, (HERE / "quadrature.asm").read_text(), clkdiv_int=clkdiv)
    enc = bench.add(QuadratureEncoder(A, B, steps, period, start=20))
    bench.start(1)
    positions = []
    while bench.t < enc.end + 20:
        bench.run(period)
        positions.append((enc.position, bench.chip.reg_read(0, X)))
    return bench, positions


class Quadrature(unittest.TestCase):

    def test_random_walk(self):
        rng = random.Random(4)
        for clkdiv in (1, 3):
            with self.subTest(clkdiv=clkdiv):
                steps = [rng.choice((1, 1, -1)) for _ in range(300)]
                bench, positions = run(steps, 8 * clkdiv, clkdiv)
                self.assertEqual(bench.errors(), [])
                self.assertEqual(positions[-1], (sum(steps), sum(steps) & 0xFFFF))
                behind = [(want - got + 0x8000) % 0x10000 - 0x8000 for want, got in positions]
                self.assertLessEqual(max(map(abs, behind)), 1, "never more than one step behind")
                self.assertEqual(bench.chip.reg_read(0, IRQ), 0, "no missed step")

    def test_backwards_below_zero(self):
        bench, positions = run([-1] * 10, 8)
        self.assertEqual(positions[-1], (-10, (-10) & 0xFFFF))

    def test_too_fast_is_reported(self):
        bench, _ = run([1] * 40, 2)
        self.assertEqual(bench.chip.reg_read(0, IRQ) & 1, 1, "missed step")

    def test_each_missed_step(self):
        """Both lines changing at once, from each of the 4 states."""
        for a, b in QuadratureEncoder.GRAY:
            with self.subTest(state=(a, b)):
                bench = Bench()
                bench.load(0, (HERE / "quadrature.asm").read_text())
                lines = bench.add(Device())
                lines.drive = {A: a, B: b}
                bench.run(4)                         # inputs through the synchronizer, as a
                bench.start(1)                       # real host takes far longer to load
                bench.run(30)
                lines.drive = {A: 1 - a, B: 1 - b}
                bench.run(30)
                self.assertEqual((bench.chip.reg_read(0, IRQ), bench.chip.reg_read(0, X)), (1, 0))


if __name__ == "__main__":
    unittest.main()
