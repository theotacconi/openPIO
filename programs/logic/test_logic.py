#!/usr/bin/env python3
"""Logic analyzer: the host writes the trigger condition at run time (ALU destination COND)
and the sample count; the samples are the 14 input pins, every 2 strobes without jitter
(each one the pins of the same clock offset). Triggers: a pin rising, a pin falling (an
inverted condition, pin 12 on ui_in), an IRQ flag the host sets."""
import random
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
sys.path.insert(0, str(HERE.parents[1] / "sim" / "model"))
from asm import condition                                # noqa: E402
from bench import Bench, Script                          # noqa: E402

PADS_IN = 0xF8FF                                         # pins with an input pad (HOST.md)


def wave(n, seed, trigger_pin=None, rise=True, at=0):
    """n clocks of random levels on the input pins; trigger_pin held at the idle level until
    clock `at`, then at the active one."""
    rnd = random.Random(seed)
    levels = []
    for t in range(n):
        v = rnd.getrandbits(16) & PADS_IN
        if trigger_pin is not None:
            v = v & ~(1 << trigger_pin) | (int(t >= at) == rise) << trigger_pin
        levels.append(v)
    return levels


def run(levels, trigger, n, clkdiv=1, irq_at=None):
    bench = Bench()
    bench.load(0, (HERE / "logic_analyzer.asm").read_text(), clkdiv_int=clkdiv)
    bench.add(Script([(t, {p: v >> p & 1 for p in range(16) if PADS_IN >> p & 1})
                      for t, v in enumerate(levels)]))
    bench.send(0, [trigger, n - 1])
    bench.start(1)
    if irq_at is not None:
        bench.run(irq_at)
        bench.chip.reg_write(0, 0x10, 1 << (8 + (trigger & 7)))       # IRQ: set the flag
    bench.run(len(levels) - bench.t)
    return bench.received[0]


class LogicAnalyzer(unittest.TestCase):

    def offset(self, samples, levels, step):
        """The clock of sample 0, the same for every sample."""
        starts = [t for t in range(len(levels) - step * len(samples))
                  if all(levels[t + step * k] == s for k, s in enumerate(samples))]
        self.assertEqual(len(starts), 1, "every sample is the pins of one clock, 2 strobes apart")
        return starts[0]

    def test_pin_rising(self):
        levels = wave(600, 1, trigger_pin=5, at=200)
        samples = run(levels, condition("gpio 5"), 100)
        self.assertEqual(len(samples), 100)
        self.assertIn(self.offset(samples, levels, 2) - 200, range(0, 5), "within 4 clocks")

    def test_pin_falling_slower(self):
        levels = wave(1500, 2, trigger_pin=12, rise=False, at=300)
        samples = run(levels, condition("!gpio 12"), 150, clkdiv=3)
        self.assertEqual(len(samples), 150, "more than the FIFO holds: the host reads along")
        self.assertIn(self.offset(samples, levels, 6) - 300, range(0, 13))

    def test_irq_flag(self):
        levels = wave(500, 3)
        samples = run(levels, condition("irq 2"), 50, irq_at=150)
        taken = self.offset(samples, levels, 2) + 2           # inputs reach the SM 2 clocks late
        self.assertIn(taken - 150, range(0, 3), "the first sample within 2 clocks of the flag")


if __name__ == "__main__":
    unittest.main()
