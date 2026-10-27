#!/usr/bin/env python3
"""USB low-speed transmitter and receiver judged by the reference USB devices: NRZI, stuffing
in its own bit time, CRC16 against CRC-16/USB, EOP timing, CRC errors."""
import random
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
sys.path.insert(0, str(HERE.parents[1] / "sim" / "model"))
from bench import Bench                                  # noqa: E402
from chip import IRQ                                     # noqa: E402
from devices import UsbRx, UsbTx, crc16_usb              # noqa: E402

DM, DP, BIT = 0, 1, 8
DATA0, DATA1 = 0xC3, 0x4B


def payloads(seed):
    rng = random.Random(seed)
    return [[0xFF, 0xFF, 0x7E], []] + [[rng.randrange(256) for _ in range(rng.randrange(1, 9))] for _ in range(4)]


class UsbTxTest(unittest.TestCase):

    def test_packets(self):
        bench = Bench()
        bench.load(0, (HERE / "usb_tx.asm").read_text(), out_base=DM, set_base=DM, set_count=2)
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


class UsbRxTest(unittest.TestCase):

    def test_packets_and_crc_errors(self):
        sent = payloads(2)
        packets = [(DATA0, p) for p in sent] + [(DATA0, [1, 2, 3], 0x1234)]   # last: bad CRC
        bench = Bench()
        bench.load(0, (HERE / "usb_rx.asm").read_text(), in_base=DM)
        tx = bench.add(UsbTx(DM, DP, BIT, packets, start=20, gap=8))
        bench.start(1)
        verdicts = []
        while bench.t < tx.end + 50:
            bench.run(1)
            flags = bench.chip.reg_read(0, IRQ)
            if flags:
                verdicts.append(flags)
                bench.chip.reg_write(0, IRQ, flags)
        self.assertEqual(verdicts, [1] * len(sent) + [2], "IRQ 0 good packet, IRQ 1 CRC error")
        words, n = [w >> 8 for w in bench.received[0]], 0
        for pid, p, *crc in packets:
            crc = crc[0] if crc else crc16_usb(p)
            expected = [0x80, pid] + p + [crc & 0xFF, crc >> 8]
            self.assertEqual(words[n:n + len(expected)], expected)
            n += len(expected)

    def test_loopback(self):
        """The transmitter program into the receiver program, on two SMs."""
        bench = Bench()
        bench.load(0, (HERE / "usb_tx.asm").read_text(), out_base=DM, set_base=DM, set_count=2)
        bench.load(1, (HERE / "usb_rx.asm").read_text(), in_base=DM)
        p = [0xFF] * 6 + [0x00, 0x42]
        bench.send(0, [0x80 | DATA0 << 8, len(p)] + p)
        bench.start(0b11)
        bench.run(1500, until=lambda: any(bench.chip.sms[1].irq_flags))
        self.assertTrue(bench.chip.sms[1].irq_flags[0])
        self.assertEqual([w >> 8 for w in bench.received[1]][2:-2], p)


if __name__ == "__main__":
    unittest.main()
