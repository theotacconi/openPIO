#!/usr/bin/env python3
"""sim/learn/learn.py against the reference devices: each recording is made by a reference UART
transmitter whose format the learner is not told; it must find that format, then its generated
programs must receive a new transmission and send one the reference receiver accepts. Frames
recorded from reference USB, CAN, MFM and biphase mark transmitters and from the chip's 10BASE-T
program: the learner must find their line code, stuffing, sync bits and CRC.

    python3 test/learn/test_learn.py
"""
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "sim" / "learn"), str(ROOT / "sim" / "model"), str(ROOT / "test" / "shared")]
from bench import Bench, Device                          # noqa: E402
from chip import IRQ                                     # noqa: E402
from devices import (BmcTx, CanNode, UartRx, UartTx, UsbTx, WiredAndBus,  # noqa: E402
                     mfm_halves, mfm_sector)
from learn import (AsyncSerial, Capture, decode, infer_async, infer_frames,  # noqa: E402
                   pau_directives, rx_program, timing, tx_program, word_value)

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

    def test_from_the_chip_recording_of_all_pins(self):
        """The same from programs/logic/record.asm: every pin recorded, the UART line taken."""
        fmt, data = (7, "odd", 2, 0, 45), values((7,), 11)
        bench = Bench(pullups=0)
        bench.load(0, (ROOT / "programs/logic/record.asm").read_text())
        tx = bench.add(transmitter(fmt, data, 11))
        bench.run(4)
        bench.start(1)
        bench.run(tx.dev.end + 200)
        p = infer_async(Capture.from_record_program(bench.received[0], PIN))
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


def bits(data: list[int], lsb_first: bool = True) -> list[int]:
    return [b >> (i if lsb_first else 7 - i) & 1 for b in data for i in range(8)]


class LearnFrames(unittest.TestCase):
    """Self-clocked frames: a burst of edges each, idle between."""

    def learn(self, levels, pin=PIN):
        return infer_frames(Capture.from_levels(levels, pin))

    def check(self, p, code, stuff, crc, sync=None):
        self.assertEqual((p.code, p.stuff), (code, stuff))
        self.assertEqual(p.crc and (p.crc.width, p.crc.poly, p.crc.init, p.crc.xorout), crc)
        if sync:
            self.assertEqual(p.sync[:len(sync)], sync)

    def test_usb(self):
        """Low speed on D-: NRZI, a 0 stuffed after six 1s (the 0xFF bytes), CRC-16/USB of the
        data packets."""
        rnd = random.Random(1)
        packets = [(0xC3, [0xFF, 0xFF] + [rnd.randrange(256) for _ in range(n)])
                   for n in (2, 4, 2, 6, 4, 1)]
        bench = Bench()
        tx = bench.add(UsbTx(PIN, PIN + 1, 40, packets, start=100, gap=16))
        bench.run(tx.end + 100)
        p = self.learn(bench.trace)
        self.check(p, "nrzi", (0, 6), (16, 0x8005, 1, 1), [0] * 7 + [1] + bits([0xC3]))
        self.assertEqual(p.crc.start, 16, "after SYNC and PID")
        self.assertAlmostEqual(p.half, 20, delta=0.2)

    def test_can(self):
        """CAN 2.0A on a wired-AND bus, a second node acknowledging: NRZ, the complement after
        five equal bits, CRC-15 from the start of frame."""
        rnd = random.Random(2)
        frames = [(rnd.randrange(2048), [rnd.randrange(256) for _ in range(n)])
                  for n in (1, 3, 3, 8, 0, 5)]
        nodes = [CanNode(50, send=frames), CanNode(50)]
        bench = Bench(pullups=1 << PIN)
        bus = bench.add(WiredAndBus([], [PIN], nodes))
        bench.run(sum(160 + 8 * len(d) for _, d in frames) * 50, until=lambda: not nodes[0].send)
        bench.run(2000)
        self.assertEqual(bus.errors, [])
        p = self.learn(bench.trace)
        self.check(p, "nrz", (1, 5), (15, 0x4599, 0, 0), [0])

    def test_mfm(self):
        """Floppy sectors: MFM (the PAU's Miller code), CRC-16/CCITT after the address mark."""
        rnd = random.Random(3)
        levels, level = [0] * 400, 0
        for n in (8, 12, 8, 16, 5):
            hs = mfm_halves(mfm_sector([rnd.randrange(256) for _ in range(n)]), level)
            levels += [h << PIN for h in hs for _ in range(20)] + [hs[-1] << PIN] * 800
            level = hs[-1]
        p = self.learn(levels)
        self.check(p, "miller", None, (16, 0x1021, 1, 0), bits([0, 0, 0, 0, 0xA1], lsb_first=False))

    def test_biphase_mark(self):
        """Frames of random bits without a CRC: biphase mark, told from its shifted codes by
        the first edge of every frame on a bit boundary."""
        rnd = random.Random(4)
        frames = [[rnd.randrange(2) for _ in range(rnd.randrange(40, 120))] for _ in range(5)]
        bench = Bench()
        tx = bench.add(BmcTx(PIN, 40, frames, start=100, gap=20))
        bench.run(tx.end + 100)
        p = self.learn(bench.trace)
        self.check(p, "bmc", None, None)
        self.assertEqual(p.frames, frames)

    def test_ethernet_from_the_chip(self):
        """10BASE-T frames sent by programs/ethernet/eth_tx.asm on the chip: Manchester, the
        preamble and SFD, the FCS (CRC-32)."""
        rnd = random.Random(5)
        bench = Bench()
        bench.load(0, (ROOT / "programs/ethernet/eth_tx.asm").read_text())
        bench.start(1)
        for n in (20, 26, 20, 32):                       # bytes after the SFD, the FCS's 4 too
            f = [rnd.randrange(256) for _ in range(n - 4)]
            bench.send(0, [len(f) // 2] + [f[i] | f[i + 1] << 8 for i in range(0, len(f), 2)])
            bench.run(200 * n, until=lambda: not bench.to_send[0])
            bench.write(0, IRQ, 1 << 8)                  # flag 0: a frame is loaded
            bench.run((n + 30) * 32)
        p = self.learn(bench.trace, 0)
        self.check(p, "manchester", None, (32, 0x04C11DB7, 1, 1), bits([0x55] * 7 + [0xD5]))
        self.assertEqual(p.crc.start, 64)
        self.assertIn(".crc width=32 poly=0x4C11DB7 init=ones xorout=1", pau_directives(p)[0])


if __name__ == "__main__":
    unittest.main()
