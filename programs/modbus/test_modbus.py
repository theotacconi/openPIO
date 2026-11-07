#!/usr/bin/env python3
"""Modbus RTU receiver fed by the reference UART transmitter: frames of 11-bit characters at
9600 baud from a 50 MHz clock (CLKDIV 651 + 11 / 256), ended by 3.5 characters of silence; gaps
of 1.5 characters inside a frame; CRC-16/MODBUS checked on the chip (good and corrupted frames);
a transmitter 2 % fast or slow; a framing error. A slave on two SMs: the receiver releases the
transmitter on the peer SM through its IRQ (irq clear peer), so a reply the host queued before the
request arrived goes out after the request's 3.5 characters of silence, with DE around it."""
import random
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
from bench import Bench                                  # noqa: E402
from devices import UartRx, UartTx, crc16_modbus         # noqa: E402

RX, TX, DE = 0, 1, 2       # RS-485 transceiver: RO, DI, DE
CHAR = 11 * 32              # clocks a character at CLKDIV 4
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


def slave(requests, replies):
    """The master sends the requests, each followed by 16 characters of silence (room for the
    reply); the host queued every reply (N, then the bytes) before the first request."""
    data, gaps = [], []
    for f in requests:
        data += f
        gaps += [0] * (len(f) - 1) + [16 * 11]
    bench = Bench(pullups=1 << RX | 1 << TX)
    bench.load(0, (HERE / "modbus_rx.asm").read_text(), clkdiv_int=4)
    bench.load(1, (HERE / "modbus_tx.asm").read_text(), clkdiv_int=4)
    bench.send(1, [w for r in replies for w in [len(r)] + r])
    master = bench.add(UartTx(RX, 32, data, start=80, gap=gaps, stop_bits=2))
    reply = bench.add(UartRx(TX, 32, stop_bits=2))
    bench.start(0b11)
    bench.run(master.end)
    ends = []                                              # each request's last stop bit ends
    t = 80
    for f in requests:
        t += len(f) * CHAR
        ends.append(t)
        t += 16 * CHAR
    return bench, reply, ends


class ModbusSlave(unittest.TestCase):

    def test_reply_after_the_request(self):
        request = adu(0x11, 0x03, 0x00, 0x6B, 0x00, 0x01)
        answer = adu(0x11, 0x03, 0x02, 0x12, 0x34)
        bench, reply, (end,) = slave([request], [answer])
        self.assertEqual(bench.received[0], words(request, GOOD))
        self.assertEqual(reply.frames, answer)
        self.assertEqual(bench.errors(), [])
        start = next(t for t, lv in bench.edges(TX) if lv == 0)
        self.assertGreaterEqual(start - end, 3.5 * CHAR)  # the silence, though queued long before
        self.assertLess(start - end, 4 * CHAR)
        de = bench.runs(DE)                                # DE high only around the reply
        self.assertEqual([lv for lv, _ in de], [0, 1, 0])
        on, off = de[0][1], de[0][1] + de[1][1]
        self.assertLess(on, start)
        self.assertGreaterEqual(off, start + len(answer) * CHAR)
        self.assertLess(off, start + len(answer) * CHAR + 32)

    def test_no_reply_then_reply(self):
        other = adu(0x22, 0x03, 0x00, 0x00, 0x00, 0x01)    # for another slave: N = 0
        request = adu(0x11, 0x06, 0x00, 0x01, 0x00, 0x03)
        bench, reply, ends = slave([other, request], [[], request])     # a write echoes it
        self.assertEqual(bench.received[0], words(other, GOOD) + words(request, GOOD))
        self.assertEqual(reply.frames, request)
        self.assertEqual(bench.errors(), [])
        start = next(t for t, lv in bench.edges(TX) if lv == 0)
        self.assertGreaterEqual(start - ends[1], 3.5 * CHAR)
        self.assertEqual([lv for lv, _ in bench.runs(DE)], [0, 1, 0])


if __name__ == "__main__":
    unittest.main()
