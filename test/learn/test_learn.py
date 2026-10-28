#!/usr/bin/env python3
"""sim/learn/learn.py against the reference devices: each recording is made by a reference UART
transmitter whose format the learner is not told; it must find that format, then its generated
programs must receive a new transmission and send one the reference receiver accepts.

    python3 test/learn/test_learn.py
"""
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "sim" / "learn"), str(ROOT / "sim" / "model"), str(ROOT / "test" / "shared")]
from bench import Bench, Device                          # noqa: E402
from devices import UartRx, UartTx                       # noqa: E402
from learn import (AsyncSerial, Capture, decode, infer_async, rx_program, timing,  # noqa: E402
                   tx_program, word_value)

PIN = 3
FORMATS = [  # (data bits, parity, stop bits, idle level, clocks a bit)
    (8, None, 1, 1, 40), (8, None, 1, 0, 37.3), (7, "even", 1, 1, 52), (8, "odd", 1, 0, 100),
    (8, None, 2, 1, 26.5), (7, None, 2, 0, 61), (9, None, 1, 1, 33), (5, "even", 2, 1, 44),
]


class Inverted(Device):
    """A device behind an inverter on `pin`: it sees and drives that pin inverted."""

    def __init__(self, dev: Device, pin: int) -> None:
        super().__init__()
        self.dev, self.pin = dev, pin
        self.drive = {p: v ^ (p == pin) for p, v in dev.drive.items()}

    def tick(self, t: int, levels: int) -> None:
        self.dev.tick(t, levels ^ 1 << self.pin)
        self.drive = {p: v ^ (p == self.pin) for p, v in self.dev.drive.items()}
        self.errors = self.dev.errors


def transmitter(fmt, data, seed, start=200):
    n, parity, stop, idle, bit = fmt
    rnd = random.Random(seed)
    gaps = [0 if rnd.random() < .5 else rnd.randrange(1, 4) for _ in data]   # some back to back
    tx = UartTx(PIN, bit, data, start=start, gap=gaps, data_bits=n, parity=parity, stop_bits=stop)
    return tx if idle else Inverted(tx, PIN)


def values(fmt, seed, count=40):
    rnd = random.Random(seed)
    return [rnd.randrange(1 << fmt[0]) for _ in range(count)]


def record(fmt, data, seed):
    bench = Bench(pullups=fmt[3] << PIN)
    tx = bench.add(transmitter(fmt, data, seed))
    bench.run((tx.dev if isinstance(tx, Inverted) else tx).end + 50)
    return Capture.from_levels(bench.trace, PIN)


class Learn(unittest.TestCase):

    def check(self, p, fmt):
        n, parity, stop, idle, bit = fmt
        self.assertEqual((p.data_bits, p.parity, p.stop_bits, p.idle), (n, parity, stop, idle))
        self.assertAlmostEqual(p.bit, bit, delta=bit * 0.01)

    def test_infers_the_format(self):
        for k, fmt in enumerate(FORMATS):
            with self.subTest(fmt=fmt):
                data = values(fmt, k)
                cap = record(fmt, data, k)
                p = infer_async(cap)
                self.check(p, fmt)
                self.assertEqual(decode(cap, p), data)

    def test_from_the_chip_recording(self):
        """The recording made by programs/logic/edges.asm on the chip, through the host."""
        fmt, data = (8, "even", 1, 1, 50), values((8,), 9)
        bench = Bench(pullups=1 << PIN)
        bench.load(0, (ROOT / "programs/logic/edges.asm").read_text(), in_base=PIN)
        tx = bench.add(transmitter(fmt, data, 9))
        bench.run(4)                                     # the inputs through the synchronizer,
        bench.start(1)                                   # as a host takes far longer to load
        bench.run(tx.end + 200)
        p = infer_async(Capture.from_edges_program(bench.received[0]))
        self.check(p, fmt)

    def test_generated_programs(self):
        """Learned from one transmission: the RX program receives another, the TX program
        sends values the reference receiver checks (format, parity, stop bits)."""
        for k, fmt in enumerate(FORMATS[:6]):
            with self.subTest(fmt=fmt):
                p = infer_async(record(fmt, values(fmt, k), k))
                h, div, frac = timing(p)
                data = values(fmt, 100 + k)
                bench = Bench(pullups=fmt[3] << PIN)
                bench.load(0, rx_program(p, PIN), clkdiv_int=div, clkdiv_frac=frac)
                tx = bench.add(transmitter(fmt, data, 100 + k, start=50))
                bench.start(1)
                bench.run((tx.dev if isinstance(tx, Inverted) else tx).end + 100)
                self.assertEqual([word_value(p, w) for w in bench.received[0]], data)
                self.assertFalse(any(bench.chip.sms[0].irq_flags[:2]), "no framing or parity error")

                if fmt[4] != int(fmt[4]):
                    continue                             # the reference receiver counts whole clocks
                bench = Bench(pullups=fmt[3] << PIN)
                bench.load(0, tx_program(p, PIN), clkdiv_int=div, clkdiv_frac=frac)
                rx = UartRx(PIN, fmt[4], data_bits=fmt[0], parity=fmt[1], stop_bits=fmt[2])
                bench.add(rx if fmt[3] else Inverted(rx, PIN))
                bench.send(0, data)
                bench.start(1)
                bench.run(round(len(data) * p.length * fmt[4]) + 500)
                self.assertEqual((rx.frames, rx.errors), (data, []))


if __name__ == "__main__":
    unittest.main()
