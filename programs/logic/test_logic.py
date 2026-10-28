#!/usr/bin/env python3
"""Logic analyzer: the host writes the trigger condition at run time (ALU destination COND)
and the sample count; the samples are the 14 input pins, every 2 strobes without jitter
(each one the pins of the same clock offset). Triggers: a pin rising, a pin falling (an
inverted condition, pin 12 on ui_in), an IRQ flag the host sets, the other SM's IRQ flag (a
capture started by the UART receiver's framing error). Edge timestamps (IN TIME): every edge
of a random waveform, the intervals exact at CLKDIV 1 and within a strobe at CLKDIV 3. A
recording of all pins (SM status: a pin changed): every change of a random waveform on the 13
input pins, with the exact time between changes."""
import random
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
sys.path.insert(0, str(HERE.parents[1] / "sim" / "model"))
from asm import condition                                # noqa: E402
from bench import Bench, Script                          # noqa: E402
from devices import UartTx                               # noqa: E402

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
        bench.write(0, 0x10, 1 << (8 + (trigger & 7)))       # IRQ: set the flag
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

    def test_peer_irq(self):
        """SM 0 receives UART bytes and raises IRQ 0 at a framing error; SM 1 captures from
        there, triggered by its peer's flag."""
        bench = Bench(pullups=1 << 3)
        bench.load(0, (HERE.parent / "uart" / "uart_rx.asm").read_text())      # RX = pin 3
        bench.load(1, (HERE / "logic_analyzer.asm").read_text())
        tx = bench.add(UartTx(3, 8, [0x11, 0x22, 0x33], start=20, gap=1, stop_low={1}))
        bench.send(1, [condition("peer_irq 0"), 39])
        bench.start(0b11)
        flagged = first = None
        while bench.t < tx.end + 100:
            bench.run(1)
            if flagged is None and bench.chip.sms[0].irq_flags[0]:
                flagged = bench.t
            if first is None and (bench.chip.sms[1].rx_level or bench.received[1]):
                first = bench.t                                # the first sample pushed
        self.assertEqual(len(bench.received[1]), 40)
        self.assertIn(first - flagged, range(1, 4), "sampling starts at once")
        self.assertEqual([w >> 3 & 1 for w in bench.received[1]], [bench.trace[t - 2] >> 3 & 1
                         for t in range(first - 1, first + 79, 2)], "the RX line, every 2 clocks")


class EdgeTimes(unittest.TestCase):

    def run_edges(self, clkdiv, seed):
        rnd = random.Random(seed)
        edges, t = [], 30
        for _ in range(60):
            t += rnd.randrange(2 * clkdiv, 40 * clkdiv)
            edges.append(t)
        bench = Bench()
        bench.load(0, (HERE / "edges.asm").read_text(), clkdiv_int=clkdiv)
        bench.add(Script([(0, {0: 0})] + [(e, {0: (k + 1) & 1}) for k, e in enumerate(edges)]))
        bench.start(1)
        bench.run(edges[-1] + 20 * clkdiv)
        level, *times = bench.received[0]
        self.assertEqual((level & 1, len(times)), (0, len(edges)), "the level, then every edge")
        return [b - a for a, b in zip(edges, edges[1:])], [(b - a) & 0xFFFF for a, b in zip(times, times[1:])]

    def test_exact_at_clkdiv_1(self):
        clocks, strobes = self.run_edges(1, 6)
        self.assertEqual(strobes, clocks)

    def test_within_a_strobe_at_clkdiv_3(self):
        clocks, strobes = self.run_edges(3, 7)
        self.assertTrue(all(abs(3 * s - c) < 3 for s, c in zip(strobes, clocks)), "within a strobe")
        self.assertLess(abs(3 * sum(strobes) - sum(clocks)), 3, "no drift")


class Record(unittest.TestCase):

    def test_every_change_of_every_pin(self):
        rnd = random.Random(10)
        changes, t, v = [], 40, 0
        for _ in range(80):
            t += rnd.randrange(3, 30)                    # 3 strobes apart at least (CLKDIV 1)
            v = (v ^ rnd.getrandbits(16)) & PADS_IN or 1
            changes.append((t, v))
        bench = Bench()
        bench.load(0, (HERE / "record.asm").read_text())
        bench.add(Script([(0, {p: 0 for p in range(16) if PADS_IN >> p & 1})] +
                         [(c, {p: v >> p & 1 for p in range(16) if PADS_IN >> p & 1}) for c, v in changes]))
        bench.run(4)                                     # the synchronizer settles
        bench.start(1)
        bench.run(t + 40)
        first, *rest = bench.received[0]
        times, pins = rest[0::2], rest[1::2]
        self.assertEqual(first, 0)
        self.assertEqual(pins, [v for _, v in changes])
        self.assertEqual([(b - a) & 0xFFFF for a, b in zip(times, times[1:])],
                         [b - a for (a, _), (b, _) in zip(changes, changes[1:])])


if __name__ == "__main__":
    unittest.main()
