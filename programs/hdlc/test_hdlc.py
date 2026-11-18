#!/usr/bin/env python3
"""HDLC transmitter judged by the reference HDLC receiver: NRZI, flags, zero insertion after
five 1s (also inside the FCS), FCS against CRC-16/X.25, frames of 1 to 20 bytes, including
bytes that look like flags; a frame the host cancels is aborted at once (seven 1s), the next
one goes out once the host has loaded the program again. The receiver program on the other SM gets the same frames from
the line, with the FCS verdict, and flags a corrupted frame; frames after an idle line of any
length (found on the FPGA board); random settings inside what the receiver promises (a
divider, a line 4 % fast or slow with jittered edges, flags and idle between frames)."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
sys.path.insert(0, str(HERE.parents[1] / "sim" / "model"))
from bench import Bench, Script                          # noqa: E402
from chip import IRQ                                     # noqa: E402
from devices import HdlcRx, crc16_x25                     # noqa: E402
from seeds import seeded                                 # noqa: E402

LINE, BIT = 0, 8                                         # pin as in the program


class Hdlc(unittest.TestCase):

    def test_frames(self):
        rng = seeded(8)
        frames = [[0x7E, 0xFF, 0xFF, 0x7E], [0x00], [0xFF] * 3] + \
                 [[rng.randrange(256) for _ in range(rng.randrange(1, 21))] for _ in range(4)]
        bench = Bench()
        bench.load(0, (HERE / "hdlc_tx.asm").read_text())
        bench.load(1, (HERE / "hdlc_rx.asm").read_text())          # same pin: in_base 0
        rx = bench.add(HdlcRx(LINE, BIT))
        for f in frames:
            bench.send(0, [len(f)] + f)
        bench.start(0b11)
        bench.run(sum(len(f) + 4 for f in frames) * 8 * BIT * 5 // 4 + 200)
        self.assertEqual((bench.errors(), rx.aborted), ([], 0))
        self.assertEqual([f["data"] for f in rx.frames], frames)
        self.assertTrue(all(f["fcs_ok"] for f in rx.frames), "FCS")
        self.assertEqual(received(bench.received[1]), [(f + crc_bytes(f), True) for f in frames])

    def test_abort(self):
        bench = Bench()
        bench.load(0, (HERE / "hdlc_tx.asm").read_text())
        rx = bench.add(HdlcRx(LINE, BIT))
        bench.send(0, [20] + list(range(20)))
        bench.start(1)
        bench.run(10 * 8 * BIT)                                    # the opening flag and ~8 bytes
        bench.write(0, IRQ, 1 << 9)                       # the host sets IRQ 1
        bench.run(40 * BIT, until=lambda: bench.chip.sms[0].irq_flags[0])
        self.assertTrue(bench.chip.sms[0].irq_flags[0])
        bench.load(0, (HERE / "hdlc_tx.asm").read_text())          # RESET, Y = the flag
        bench.start(1)
        bench.send(0, [2, 0xA5, 0x5A])
        bench.run(8 * 8 * BIT)
        self.assertEqual((rx.aborted, [f["data"] for f in rx.frames]), (1, [[0xA5, 0x5A]]))
        self.assertEqual(bench.errors(), [])

    def test_receiver_flags_a_bad_fcs(self):
        """A reference line with idle, a good frame, a corrupted one, flags back to back."""
        good, bad = [0x7E, 0x01, 0xFF], [0x55, 0xAA]
        bits = (idle(20) + flag() + stuffed(good + crc_bytes(good)) + flag() + flag()
                + stuffed(bad + [0x12, 0x34]) + flag() + idle(20))
        bench = Bench()
        bench.load(1, (HERE / "hdlc_rx.asm").read_text())
        bench.add(Script(nrzi(bits)))
        bench.start(0b10)
        bench.run(len(bits) * BIT + 100)
        self.assertEqual(received(bench.received[1]),
                         [(good + crc_bytes(good), True), (bad + [0x12, 0x34], False)])


    def test_receiver_after_any_idle(self):
        """Frames after an idle line of 7 to 30 bit times, every length a stuffing slot could
        fall on: found on the board, where frames come whenever the host sends them. A
        destuffer counting through the idle deleted the first 0 of a flag after 6k + 5 1s."""
        good = [0x41, 0x7E, 0xFF]
        bits = []
        for n in range(7, 31):
            bits += idle(n) + flag() + stuffed(good + crc_bytes(good)) + flag()
        bits += idle(3)
        bench = Bench()
        bench.load(1, (HERE / "hdlc_rx.asm").read_text())
        bench.add(Script(nrzi(bits)))
        bench.start(0b10)
        bench.run(len(bits) * BIT + 100)
        self.assertEqual(received(bench.received[1]), [(good + crc_bytes(good), True)] * 24)

    def test_receiver_sweep(self):
        """Random settings inside what hdlc_rx.asm promises: any divider (fractional from 2),
        a line up to 4 % fast or slow, every edge up to 1/16 bit off, frames of 1 to 20 bytes
        rich in 0x00, 0xFF and 0x7E, back to back, with extra flags or an idle line between.
        Measured: 5 % fast and 7 % slow pass at every divider (the PAU samples a level code
        H strobes after the strobe that saw an edge: after the bit's middle); 6 % fast fails
        on a flag's six 1s."""
        rnd = seeded(18)
        for trial in range(4):
            clkdiv = rnd.choice([1, 2, rnd.randrange(3, 20)])
            frac = rnd.choice([0, rnd.randrange(256)]) if clkdiv > 1 else 0
            off = rnd.uniform(-0.04, 0.04)
            frames = [[rnd.choice([0x00, 0xFF, 0x7E, rnd.randrange(256)])
                       for _ in range(rnd.randrange(1, 21))] for _ in range(4)]
            bits = idle(rnd.randrange(7, 40))
            for f in frames:
                bits += flag() + stuffed(f + crc_bytes(f)) + flag()
                bits += flag() * rnd.choice([0, 0, 1, 2]) + idle(rnd.choice([0, 0, rnd.randrange(7, 30)]))
            bits += idle(10)
            bit = BIT * (clkdiv + frac / 256) * (1 + off)
            with self.subTest(trial=trial, clkdiv=clkdiv, frac=frac, off=f"{off:+.2%}"):
                bench = Bench()
                bench.load(1, (HERE / "hdlc_rx.asm").read_text(), clkdiv_int=clkdiv, clkdiv_frac=frac)
                bench.add(Script(nrzi(bits, bit=bit, jitter=bit / 16, rnd=rnd)))
                bench.start(0b10)
                bench.run(int(len(bits) * bit) + 100)
                self.assertEqual(received(bench.received[1]), [(f + crc_bytes(f), True) for f in frames])


def crc_bytes(data):
    fcs = crc16_x25(data)
    return [fcs & 0xFF, fcs >> 8]


def received(words):
    """RX FIFO words -> [(bytes, FCS ok)]."""
    frames, data = [], []
    for w in words:
        if w & 1:
            frames.append((data, bool(w & 2)))
            data = []
        else:
            data.append(w >> 8)
    return frames


def idle(n):
    return [1] * n


def flag():
    return [0, 1, 1, 1, 1, 1, 1, 0]


def stuffed(data):
    bits, ones = [], 0
    for b in [byte >> i & 1 for byte in data for i in range(8)]:
        bits.append(b)
        ones = ones + 1 if b else 0
        if ones == 5:
            bits.append(0)
            ones = 0
    return bits


def nrzi(bits, start=10, bit=BIT, jitter=0, rnd=None):
    """Script steps: a 0 is a transition; `bit` clocks a bit (fractional: a line fast or slow),
    each edge moved by up to +- `jitter` clocks (`rnd`)."""
    steps, level, t = [(0, {LINE: 1})], 1, start
    for b in bits:
        level ^= 1 - b
        steps.append((round(t + (rnd.uniform(-jitter, jitter) if jitter else 0)), {LINE: level}))
        t += bit
    return steps


if __name__ == "__main__":
    unittest.main()
