#!/usr/bin/env python3
"""Modbus RTU receiver fed by the reference UART transmitter: frames of 11-bit characters at
9600 baud from a 50 MHz clock (CLKDIV 651 + 11 / 256), ended by 3.5 characters of silence; gaps
of 1.5 characters inside a frame; CRC-16/MODBUS checked on the chip (good and corrupted frames);
a transmitter 2 % fast or slow; a framing error."""
import random
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
from bench import Bench                                  # noqa: E402
from devices import UartTx, crc16_modbus                 # noqa: E402

RX = 0
GOOD, BAD = 0x0001, 0x0003


def adu(*pdu):
    """A frame with its CRC, low byte first."""
    crc = crc16_modbus(list(pdu))
    return list(pdu) + [crc & 0xFF, crc >> 8]


def words(frame, mark):
    return [b << 8 for b in frame] + [mark]


def run(frames, clkdiv=(4, 0), bit=None, inner_gap=0.0, stop_low=frozenset()):
    """Send the frames as one line: characters back to back (plus inner_gap characters), frames
    4 characters apart. Returns the bench and the words of the RX FIFO."""
    div = clkdiv[0] + clkdiv[1] / 256
    bit = bit or 8 * div
    data, gaps = [], []
    for f in frames:
        data += f
        gaps += [inner_gap * 11] * (len(f) - 1) + [4 * 11]
    bench = Bench(pullups=1 << RX)
    bench.load(0, (HERE / "modbus_rx.asm").read_text(), clkdiv_int=clkdiv[0], clkdiv_frac=clkdiv[1])
    tx = bench.add(UartTx(RX, bit, data, start=round(20 * div), gap=gaps, stop_bits=2, stop_low=stop_low))
    bench.start(1)
    bench.run(tx.end + round(8 * div))
    return bench, bench.received[0]


class ModbusRx(unittest.TestCase):

    def test_9600_baud_from_50_mhz(self):
        request, reply = adu(0x11, 0x03, 0x00, 0x6B, 0x00, 0x03), adu(0x11, 0x06, 0x00, 0x01)
        reply[2] ^= 0x10                                   # corrupted on the line
        bench, got = run([request, reply], clkdiv=(651, 11))
        self.assertEqual(got, words(request, GOOD) + words(reply, BAD))
        self.assertFalse(bench.chip.sms[0].irq_flags[0])

    def test_frames_and_gaps(self):
        rnd = random.Random(5)
        frames = [adu(*[rnd.randrange(256) for _ in range(n)]) for n in (1, 6, 20)]
        for inner_gap in (0.0, 1.5):
            with self.subTest(inner_gap=inner_gap):
                bench, got = run(frames, inner_gap=inner_gap)
                self.assertEqual(got, [w for f in frames for w in words(f, GOOD)])

    def test_baud_tolerance(self):
        frame = adu(0x01, 0x10, 0x00, 0x20, 0x00, 0x02, 0x04, 0xFF, 0x00, 0x55, 0xAA)
        for bit in (31.36, 32.64):                         # receiver: 32 clocks a bit
            with self.subTest(bit=bit):
                bench, got = run([frame, frame], bit=bit)
                self.assertEqual(got, 2 * words(frame, GOOD))

    def test_framing_error(self):
        frame = adu(0x01, 0x05, 0x00, 0x13, 0xFF, 0x00)
        bench, got = run([frame], stop_low={2})
        self.assertTrue(bench.chip.sms[0].irq_flags[0])


if __name__ == "__main__":
    unittest.main()
