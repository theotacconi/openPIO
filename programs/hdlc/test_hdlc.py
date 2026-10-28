#!/usr/bin/env python3
"""HDLC transmitter judged by the reference HDLC receiver: NRZI, flags, zero insertion after
five 1s (also inside the FCS), FCS against CRC-16/X.25, frames of 1 to 20 bytes, including
bytes that look like flags; a frame the host cancels is aborted at once (seven 1s), the next
one goes out once the host has loaded the program again. The receiver program on the other SM gets the same frames from
the line, with the FCS verdict, and flags a corrupted frame."""
import random
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
sys.path.insert(0, str(HERE.parents[1] / "sim" / "model"))
from bench import Bench, Script                          # noqa: E402
from chip import IRQ                                     # noqa: E402
from devices import HdlcRx, crc16_x25                     # noqa: E402

LINE, BIT = 0, 8                                         # pin as in the program


class Hdlc(unittest.TestCase):

    def test_frames(self):
        rng = random.Random(8)
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
        bench.chip.reg_write(0, IRQ, 1 << 9)                       # the host sets IRQ 1
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


def nrzi(bits, start=10):
    """Script steps: a 0 is a transition."""
    steps, level, t = [(0, {LINE: 1})], 1, start
    for b in bits:
        level ^= 1 - b
        steps.append((t, {LINE: level}))
        t += BIT
    return steps


if __name__ == "__main__":
    unittest.main()
