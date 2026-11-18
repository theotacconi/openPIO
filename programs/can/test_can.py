#!/usr/bin/env python3
"""CAN transmitter and receiver against reference CAN nodes on a wired-AND bus: frames, ACK,
arbitration lost at the exact bit, CRC errors; the receiver over random settings inside what it
promises (a divider from 4, the sender's oscillator up to 0.75 % off, frames with the most
stuffing), its ACK within CAN's window.
sigrok's CAN decoder reads the same frames on the bus (identifier, data, stuffing, ACK) with no
warning."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
from bench import Bench                                  # noqa: E402
from devices import WiredAndBus, CanNode, can_bits            # noqa: E402
from sigrok import can as sigrok_can                     # noqa: E402
from seeds import seeded                                 # noqa: E402

TX, RX, BIT = 0, 1, 8  # pins as in the program


def sigrok_frames(bench, bit=BIT):
    """[(id, data, acknowledged)] as sigrok reads the bus; no warning."""
    frames, warnings = sigrok_can(bench, RX, bit)
    assert warnings == [], warnings
    return [(f["id"], f["data"], f["ack"]) for f in frames]


def frame_words(ident, data):
    bits = can_bits(ident, data)
    bits += [0] * (-len(bits) % 16)
    return [len(can_bits(ident, data))] + [sum(b << (15 - i) for i, b in enumerate(bits[k:k + 16]))
                                          for k in range(0, len(bits), 16)]


def tx_bench(nodes, frames):
    bench = Bench(pullups=1 << RX | 1 << TX)               # TXD pulled up, as on a transceiver
    bench.load(0, (HERE / "can_tx.asm").read_text())
    bus = bench.add(WiredAndBus([TX], [RX], nodes))
    for f in frames:
        bench.send(0, frame_words(*f))
    bench.start(1)
    return bench, bus


class CanTx(unittest.TestCase):

    def test_frames_acknowledged(self):
        rng = seeded(6)
        frames = [(rng.randrange(0x7F0), [rng.randrange(256) for _ in range(rng.randrange(9))])
                  for _ in range(30)]                   # many CRCs: stuffing next to the delimiter
        frames[1] = (0x7EF, [0xFF])               # long recessive runs: stuffing (0x7F0 up: not legal)
        listener = CanNode(BIT)
        bench, bus = tx_bench([listener], [])
        irqs = []
        for f in frames:
            bench.send(0, frame_words(*f))
            bench.run(2000, until=lambda: any(bench.chip.sms[0].irq_flags))
            irqs.append(bench.chip.reg_read(0, 0x10))
            bench.write(0, 0x10, 0xFF)
        self.assertEqual(bench.errors(), [])
        self.assertEqual([(f["id"], f["data"]) for f in listener.frames], frames)
        self.assertTrue(all(f["crc_ok"] for f in listener.frames))
        self.assertEqual(irqs, [1] * len(frames), "IRQ 0: acknowledged")
        self.assertEqual(sigrok_frames(bench), [(i, d, True) for i, d in frames], "sigrok")

    def test_no_ack(self):
        bench, _ = tx_bench([CanNode(BIT, ack=False)], [(0x100, [1, 2])])
        bench.run(2000, until=lambda: any(bench.chip.sms[0].irq_flags))
        self.assertTrue(bench.chip.sms[0].irq_flags[1])
        bench.run(100)                                         # to the end of the ACK slot
        self.assertEqual(sigrok_frames(bench)[0], (0x100, [1, 2], False), "sigrok: no ACK")

    def test_lost_arbitration(self):
        rival = CanNode(BIT, send=[(0x0F0, [0xAB])], idle=0)   # lower ID; joins our SOF
        listener = CanNode(BIT)
        bench, bus = tx_bench([rival, listener], [(0x0F1, [0xCD])])
        bench.run(3000, until=lambda: any(bench.chip.sms[0].irq_flags))
        self.assertTrue(bench.chip.sms[0].irq_flags[2], "IRQ 2: lost")
        bench.run(400)
        self.assertEqual(bench.errors(), [])
        self.assertEqual(listener.frames[0], {"id": 0x0F0, "data": [0xAB], "crc_ok": True},
                         "the winner's frame is intact")
        self.assertEqual(rival.sent, [(0x0F0, True)])
        self.assertEqual(sigrok_frames(bench), [(0x0F0, [0xAB], True)], "sigrok: the winner's frame")


class CanRx(unittest.TestCase):

    def test_frames(self):
        rng = seeded(8)
        frames = [(rng.randrange(0x7F0), [rng.randrange(256) for _ in range(n)]) for n in (0, 3, 8)]
        sender = CanNode(4 * BIT, send=frames, idle=0)         # waits for 11 idle bits
        bench = Bench(pullups=1 << RX | 1 << TX)
        bench.load(0, (HERE / "can_rx.asm").read_text(), clkdiv_int=4)   # CLKDIV from 4
        bench.add(WiredAndBus([TX], [RX], [sender]))
        bench.start(1)
        irqs = []
        for _ in frames:
            bench.run(12000, until=lambda: any(bench.chip.sms[0].irq_flags))
            irqs.append(bench.chip.reg_read(0, 0x10))
            bench.write(0, 0x10, 0xFF)
        bench.run(800)
        self.assertEqual(bench.errors(), [])
        self.assertEqual(irqs, [1] * len(frames), "IRQ 0: CRC right")
        self.assertEqual(sender.sent, [(f[0], True) for f in frames], "acknowledged by the program")
        words, got = list(bench.received[0]), []
        while words:
            head, dlc = words.pop(0), words.pop(0) & 0xF
            got.append((head >> 3 & 0x7FF, [words.pop(0) & 0xFF for _ in range(dlc)]))
        self.assertEqual(got, frames)
        self.assertEqual(sigrok_frames(bench, 4 * BIT), [(i, d, True) for i, d in frames], "sigrok")

    def test_sweep(self):
        """Random settings inside what can_rx.asm promises: a divider from 4 (fractional),
        a sender whose oscillator is up to 0.75 % fast or slow (crystals: 0.01 %), identifiers 0
        and 0x7EF and data of 0x00 and 0xFF among the frames (the longest runs, stuffed). Every
        frame read, its CRC right, acknowledged, the ACK's edges from 1/8 bit early to 1/4 late
        (CanNode). Measured: frames are read up to 3 % off; from 1 % slow the ACK, timed from
        the last edge up to 6 bits before, can start more than 1/8 bit early. Found the ACK 1/4
        bit early at every divider from 4: timed for CLKDIV 1, where the input's 3 clocks of
        delay made up for 2 strobes."""
        rnd = seeded(16)
        for trial in range(4):
            clkdiv = rnd.choice([4, rnd.randrange(4, 26)])     # 6.25: 1 Mbit/s from 50 MHz
            frac = rnd.choice([0, rnd.randrange(256)])
            off = rnd.uniform(-0.0075, 0.0075)
            frames = [(rnd.choice([0, 0x7EF, rnd.randrange(0x7F0)]),
                       [rnd.choice([0x00, 0xFF, rnd.randrange(256)]) for _ in range(rnd.randrange(9))])
                      for _ in range(3)]
            with self.subTest(trial=trial, clkdiv=clkdiv, frac=frac, off=f"{off:+.2%}"):
                sender = CanNode(BIT * (clkdiv + frac / 256) * (1 + off), send=frames, idle=0)
                bench = Bench(pullups=1 << RX | 1 << TX)
                bench.load(0, (HERE / "can_rx.asm").read_text(), clkdiv_int=clkdiv, clkdiv_frac=frac)
                bench.add(WiredAndBus([TX], [RX], [sender]))
                bench.start(1)
                irqs = []
                for _ in frames:
                    bench.run(400 * BIT * (clkdiv + 1), until=lambda: any(bench.chip.sms[0].irq_flags))
                    irqs.append(bench.chip.reg_read(0, 0x10))
                    bench.write(0, 0x10, 0xFF)
                bench.run(30 * BIT * (clkdiv + 1))
                self.assertEqual(bench.errors(), [])
                self.assertEqual(irqs, [1] * len(frames), "IRQ 0: CRC right")
                self.assertEqual(sender.sent, [(f[0], True) for f in frames], "acknowledged")
                words, got = list(bench.received[0]), []
                while words:
                    head, dlc = words.pop(0), words.pop(0) & 0xF
                    got.append((head >> 3 & 0x7FF, [words.pop(0) & 0xFF for _ in range(dlc)]))
                self.assertEqual(got, frames)


if __name__ == "__main__":
    unittest.main()
