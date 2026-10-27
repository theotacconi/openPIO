#!/usr/bin/env python3
"""CAN transmitter and receiver against reference CAN nodes on a wired-AND bus: frames, ACK,
arbitration lost at the exact bit, CRC errors."""
import random
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
from bench import Bench                                  # noqa: E402
from devices import CanBus, CanNode, can_bits            # noqa: E402

TX, RX, BIT = 0, 1, 8


def frame_words(ident, data):
    bits = can_bits(ident, data)
    bits += [0] * (-len(bits) % 16)
    return [len(can_bits(ident, data))] + [sum(b << (15 - i) for i, b in enumerate(bits[k:k + 16]))
                                          for k in range(0, len(bits), 16)]


def tx_bench(nodes, frames):
    bench = Bench(pullups=1 << RX | 1 << TX)               # TXD pulled up, as on a transceiver
    bench.load(0, (HERE / "can_tx.asm").read_text(), out_base=TX, set_base=TX, set_count=1,
               in_base=RX, shift_out_right=False)
    bus = bench.add(CanBus([TX], [RX], nodes))
    for f in frames:
        bench.send(0, frame_words(*f))
    bench.start(1)
    return bench, bus


class CanTx(unittest.TestCase):

    def test_frames_acknowledged(self):
        rng = random.Random(6)
        frames = [(rng.randrange(2048), [rng.randrange(256) for _ in range(n)]) for n in (0, 1, 8, 8)]
        frames[1] = (0x7F0, [0xFF])               # long recessive runs: stuffing
        listener = CanNode(BIT)
        bench, bus = tx_bench([listener], [])
        irqs = []
        for f in frames:
            bench.send(0, frame_words(*f))
            bench.run(2000, until=lambda: any(bench.chip.sms[0].irq_flags))
            irqs.append(bench.chip.reg_read(0, 0x10))
            bench.chip.reg_write(0, 0x10, 0xFF)
        self.assertEqual(bench.errors(), [])
        self.assertEqual([(f["id"], f["data"]) for f in listener.frames], frames)
        self.assertTrue(all(f["crc_ok"] for f in listener.frames))
        self.assertEqual(irqs, [1] * len(frames), "IRQ 0: acknowledged")

    def test_no_ack(self):
        bench, _ = tx_bench([CanNode(BIT, ack=False)], [(0x100, [1, 2])])
        bench.run(2000, until=lambda: any(bench.chip.sms[0].irq_flags))
        self.assertTrue(bench.chip.sms[0].irq_flags[1])

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


class CanRx(unittest.TestCase):

    def test_frames(self):
        rng = random.Random(8)
        frames = [(rng.randrange(2048), [rng.randrange(256) for _ in range(n)]) for n in (0, 3, 8)]
        sender = CanNode(BIT, send=frames, idle=0)             # waits for 11 idle bits
        bench = Bench(pullups=1 << RX | 1 << TX)
        bench.load(0, (HERE / "can_rx.asm").read_text(), set_base=TX, set_count=1, in_base=RX,
                   shift_in_right=False)
        bench.add(CanBus([TX], [RX], [sender]))
        bench.start(1)
        irqs = []
        for _ in frames:
            bench.run(3000, until=lambda: any(bench.chip.sms[0].irq_flags))
            irqs.append(bench.chip.reg_read(0, 0x10))
            bench.chip.reg_write(0, 0x10, 0xFF)
        bench.run(200)
        self.assertEqual(bench.errors(), [])
        self.assertEqual(irqs, [1] * len(frames), "IRQ 0: CRC right")
        self.assertEqual(sender.sent, [(f[0], True) for f in frames], "acknowledged by the program")
        words, got = list(bench.received[0]), []
        while words:
            head, dlc = words.pop(0), words.pop(0) & 0xF
            got.append((head >> 3 & 0x7FF, [words.pop(0) & 0xFF for _ in range(dlc)]))
        self.assertEqual(got, frames)


if __name__ == "__main__":
    unittest.main()
