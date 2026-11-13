#!/usr/bin/env python3
"""4b5b in software through a jump table, BMC on the PAU, decoded by a reference biphase mark
receiver: every nibble comes back, symbols back to back on the line (no sigrok decoder takes a
stream without the K-codes the program does not send). The receive path from a reference BMC
transmitter: USB Power Delivery messages (preamble, SOP K-codes, header, data objects and
CRC-32 from zlib.crc32 in 4b5b, EOP) with the transmitter 6 % fast or slow, every symbol in
order, and the same messages read by sigrok's USB PD decoder, which checks the CRC itself; a
frame the host does not read in time is flagged (PAU overrun)."""
import random
import sys
import unittest
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
sys.path.insert(0, str(HERE.parents[1] / "sim" / "model"))
from bench import Bench                                  # noqa: E402
from devices import BmcRx, BmcTx                         # noqa: E402
from sigrok import usb_pd as sigrok_usb_pd               # noqa: E402

CODES = [0b11110, 0b01001, 0b10100, 0b10101, 0b01010, 0b01011, 0b01110, 0b01111,
         0b10010, 0b10011, 0b10110, 0b10111, 0b11010, 0b11011, 0b11100, 0b11101]
PIN, BIT = 0, 8
SYNC1, SYNC2, EOP = 0b11000, 0b10001, 0b01101


def pd_frame(nibbles):
    """Symbols of a USB PD frame: SOP (Sync-1 x 3, Sync-2), the 4b5b nibbles, EOP."""
    return [SYNC1] * 3 + [SYNC2] + [CODES[n] for n in nibbles] + [EOP]


def pd_message(mtype, objects=(), msg_id=0):
    """(header, objects, CRC, symbols) of a USB PD message: the header (message type, PD 3.0,
    sink, UFP, message ID, number of objects), the 32-bit objects, the CRC-32 over both, all
    little-endian and sent low nibble first."""
    header = mtype | 2 << 6 | msg_id << 9 | len(objects) << 12
    data = header.to_bytes(2, "little") + b"".join(o.to_bytes(4, "little") for o in objects)
    crc = zlib.crc32(data)
    data += crc.to_bytes(4, "little")
    return header, list(objects), crc, pd_frame([n for b in data for n in (b & 15, b >> 4)])


def line_bits(symbols):
    """Preamble (64 alternating bits from 0), then the symbols LSB first."""
    return [k & 1 for k in range(64)] + [s >> k & 1 for s in symbols for k in range(5)]


class FourB5B(unittest.TestCase):

    def test_nibbles(self):
        nibbles = list(range(16)) + [random.Random(3).randrange(16) for _ in range(30)]
        bench = Bench()
        program = bench.load(0, (HERE / "fourb5b_tx.asm").read_text())
        rx = bench.add(BmcRx(PIN, BIT))
        bench.send(0, [2 * n for n in nibbles])
        bench.start(1)
        bench.run(len(nibbles) * 5 * BIT + 100)
        self.assertEqual(len(program), 39)
        self.assertEqual(bench.errors(), [])
        self.assertEqual(len(rx.frames), 1, "one unbroken stream")
        bits = rx.frames[0]
        symbols = [sum(bits[i + k] << k for k in range(5)) for i in range(0, len(bits) - 4, 5)]
        self.assertEqual([CODES.index(s) for s in symbols], nibbles)


class FourB5BRx(unittest.TestCase):

    def run_rx(self, frames, bit=32, host_word_clocks=0):
        bench = Bench(host_word_clocks=host_word_clocks)
        bench.load(0, (HERE / "fourb5b_rx.asm").read_text(), clkdiv_int=4)       # 32 clocks a bit
        tx = bench.add(BmcTx(PIN, bit, [line_bits(f) for f in frames], start=40, gap=6, end_edge=True))
        bench.start(1)
        bench.run(tx.end + 200)
        return bench

    def test_frames(self):
        rnd = random.Random(8)
        messages = [pd_message(1),                                      # GoodCRC
                    pd_message(1, [0x0801912C, 0x0002D12C], 1),         # Source_Capabilities
                    pd_message(2, [0x1304B12C], 2),                     # Request
                    pd_message(15, [rnd.randrange(1 << 32) for _ in range(7)], 3)]   # VDM
        frames = [m[3] for m in messages]
        for bit in (30, 32, 34):
            with self.subTest(bit=bit):
                bench = self.run_rx(frames, bit)
                self.assertEqual([w >> 11 for w in bench.received[0]], [s for f in frames for s in f])
                self.assertEqual([w & 0x7FF for w in bench.received[0]], [0] * sum(map(len, frames)))
                self.assertFalse(bench.chip.sms[0].irq_flags[0])
                self.assertEqual(sigrok_usb_pd(bench, PIN, bit), ([m[:3] for m in messages], []), "sigrok")

    def test_overrun(self):
        """The host reads a word every 400 clocks, a symbol comes every 160: the RX FIFO fills,
        then the PAU's queue overflows. Flagged once the frame is over and the FIFO drained."""
        frame = pd_frame([k % 16 for k in range(300)])
        bench = self.run_rx([frame], host_word_clocks=400)
        sm = bench.chip.sms[0]
        bench.run(400 * 140, until=lambda: sm.irq_flags[0])
        self.assertTrue(sm.irq_flags[0])
        self.assertLess(len(bench.received[0]), len(frame), "symbols were lost")


if __name__ == "__main__":
    unittest.main()
