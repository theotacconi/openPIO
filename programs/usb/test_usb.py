#!/usr/bin/env python3
"""USB low-speed transmitter and receiver judged by the reference USB devices: NRZI, stuffing
in its own bit time, CRC16 against CRC-16/USB, EOP timing, CRC errors, a packet broken by SE1
(an illegal line state) dropped and the next one received; the receiver over random settings
inside what it promises (a divider, the transmitter up to 2 % off, edges off by up to 1/16 bit,
payloads of 0 to 8 bytes rich in 0x00 and 0xFF). sigrok's USB decoders read the same packets,
and check their CRC16 themselves."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
sys.path.insert(0, str(HERE.parents[1] / "sim" / "model"))
from bench import Bench                                  # noqa: E402
from chip import IRQ                                     # noqa: E402
from devices import SE1, UsbRx, UsbTx, crc16_usb, usb_states   # noqa: E402
from sigrok import usb as sigrok_usb                     # noqa: E402
from seeds import seeded                                 # noqa: E402

DM, DP, BIT = 0, 1, 8
DATA0, DATA1 = 0xC3, 0x4B


def payloads(seed):
    rng = seeded(seed)
    return [[0xFF, 0xFF, 0x7E], []] + [[rng.randrange(256) for _ in range(rng.randrange(1, 9))] for _ in range(4)]


class UsbTxTest(unittest.TestCase):

    def test_packets(self):
        bench = Bench()
        bench.load(0, (HERE / "usb_tx.asm").read_text())
        rx = bench.add(UsbRx(DM, DP, BIT))
        sent = payloads(1)
        for i, p in enumerate(sent):
            bench.send(0, [0x80 | (DATA1 if i % 2 else DATA0) << 8, len(p)] + p)
        bench.start(1)
        bench.run(6000)
        self.assertEqual(bench.errors(), [])
        self.assertEqual(rx.skew, 0)
        self.assertEqual([p["data"][:-2] for p in rx.packets], sent)
        self.assertTrue(all(p["crc_ok"] and p["sync"] == 0x80 for p in rx.packets))
        self.assertEqual(sigrok_usb(bench, DP, DM, BIT),
                         [{"sync": True, "pid": "DATA1" if i % 2 else "DATA0", "data": p, "crc_ok": True}
                          for i, p in enumerate(sent)], "sigrok")


class UsbRxTest(unittest.TestCase):

    def test_packets_and_crc_errors(self):
        sent = payloads(2)
        packets = [(DATA0, p) for p in sent] + [(DATA0, [1, 2, 3], 0x1234)]   # last: bad CRC
        bench = Bench()
        bench.load(0, (HERE / "usb_rx.asm").read_text())
        tx = bench.add(UsbTx(DM, DP, BIT, packets, start=20, gap=8))
        bench.start(1)
        verdicts = []
        while bench.t < tx.end + 50:
            bench.run(1)
            flags = bench.chip.reg_read(0, IRQ)
            if flags:
                verdicts.append(flags)
                bench.write(0, IRQ, flags)
        self.assertEqual(verdicts, [1] * len(sent) + [2], "IRQ 0 good packet, IRQ 1 CRC error")
        words, n = [w >> 8 for w in bench.received[0]], 0
        for pid, p, *crc in packets:
            crc = crc[0] if crc else crc16_usb(p)
            expected = [0x80, pid] + p + [crc & 0xFF, crc >> 8]
            self.assertEqual(words[n:n + len(expected)], expected)
            n += len(expected)
        self.assertEqual(sigrok_usb(bench, DP, DM, BIT),
                         [{"sync": True, "pid": "DATA0", "data": p, "crc_ok": not crc}
                          for _, p, *crc in packets], "sigrok: the CRC errors are the same")

    def test_se1(self):
        broken = usb_states(DATA0, [0x12, 0x34, 0x56])
        broken[30:32] = [SE1, SE1]
        bench = Bench()
        bench.load(0, (HERE / "usb_rx.asm").read_text())
        tx = bench.add(UsbTx(DM, DP, BIT, [broken, (DATA1, [0x9A])], start=20, gap=8))
        bench.start(1)
        bench.run(tx.end + 50)
        self.assertEqual([f for f in bench.chip.sms[0].irq_flags[:3]], [True, False, True],
                         "IRQ 2 for the SE1, IRQ 0 for the next packet")
        self.assertEqual([w >> 8 for w in bench.received[0]][-5:], [0x80, DATA1, 0x9A] + [
            crc16_usb([0x9A]) & 0xFF, crc16_usb([0x9A]) >> 8])

    def test_sweep(self):
        """Random settings inside what usb_rx.asm promises: any divider (fractional from 2 up),
        a transmitter up to 2 % fast or slow (USB low speed allows 1.5 %), every state's edge off
        by up to 1/16 bit, payloads of 0 to 8 bytes rich in 0x00 (a transition every bit) and
        0xFF (stuffed bits). Every byte and every CRC verdict right. The limit is the longest
        run, 6 bits with no edge, sampled 4 strobes after the last edge: edges off by 1/8 bit
        (1 strobe) and 1.6 % fast lose a bit there now and then."""
        rnd = seeded(14)
        for trial in range(5):
            clkdiv = rnd.choice([1, 2, rnd.randrange(3, 12)])
            frac = rnd.choice([0, rnd.randrange(256)]) if clkdiv > 1 else 0
            off = rnd.uniform(-0.02, 0.02)
            bit = BIT * (clkdiv + frac / 256)
            packets = [(rnd.choice([DATA0, DATA1]),
                        [rnd.choice([0x00, 0xFF, rnd.randrange(256)]) for _ in range(rnd.randrange(9))])
                       for _ in range(4)]
            with self.subTest(trial=trial, clkdiv=clkdiv, frac=frac, off=f"{off:+.2%}"):
                bench = Bench()
                bench.load(0, (HERE / "usb_rx.asm").read_text(), clkdiv_int=clkdiv, clkdiv_frac=frac)
                tx = bench.add(UsbTx(DM, DP, bit * (1 + off), packets, start=20 + 3 * (clkdiv + 1),
                                     gap=8, jitter=bit / 16, seed=rnd.randrange(1 << 30)))
                bench.start(1)
                verdicts = []
                while bench.t < tx.end + 50 + 2 * bit:
                    bench.run(1)
                    flags = bench.chip.reg_read(0, IRQ)
                    if flags:
                        verdicts.append(flags)
                        bench.write(0, IRQ, flags)
                self.assertEqual(verdicts, [1] * len(packets), "IRQ 0: every CRC16 right")
                self.assertEqual([w >> 8 for w in bench.received[0]],
                                 [x for pid, p in packets
                                  for x in [0x80, pid] + p + [crc16_usb(p) & 0xFF, crc16_usb(p) >> 8]])

    def test_loopback(self):
        """The transmitter program into the receiver program, on two SMs."""
        bench = Bench()
        bench.load(0, (HERE / "usb_tx.asm").read_text())
        bench.load(1, (HERE / "usb_rx.asm").read_text())
        p = [0xFF] * 6 + [0x00, 0x42]
        bench.send(0, [0x80 | DATA0 << 8, len(p)] + p)
        bench.start(0b11)
        bench.run(1500, until=lambda: any(bench.chip.sms[1].irq_flags))
        self.assertTrue(bench.chip.sms[1].irq_flags[0])
        self.assertEqual([w >> 8 for w in bench.received[1]][2:-2], p)


if __name__ == "__main__":
    unittest.main()
