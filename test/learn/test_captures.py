"""test_captures.py - The learner on real recordings: board/captures/, a logic analyzer on the
Tang Nano 20K build (board/captures/README.md). What each one must be learned as, without a
board or sigrok."""

import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sim" / "learn"))

from learn import (Capture, crc_check, infer_async, infer_clocked, infer_frames,  # noqa: E402
                   infer_pulses, pulses_check)

CAPTURES = ROOT / "board" / "captures"


def byte_lsb(bits):
    return sum(b << k for k, b in enumerate(bits))


def word_msb(bits):
    return int("".join(map(str, bits)), 2)


class Captures(unittest.TestCase):

    def test_uart(self):
        """The chip's uart_tx on pin 0 at 27 MHz / (29 + 76/256): 8N1, 115200 baud."""
        p = infer_async(Capture.from_sigrok(CAPTURES / "uart_115200.sr", 27e6)["D0"])
        self.assertEqual((p.data_bits, p.parity, p.stop_bits, p.idle), (8, None, 1, 1))
        self.assertAlmostEqual(p.bit, 27e6 / 115200, delta=0.5)

    def test_hdlc(self):
        """hdlc_tx on SM 0, six frames, 9600 bit/s: NRZI, a 0 after five ones, the flag, the
        CRC-16 of X.25."""
        p = infer_frames(Capture.from_sigrok(CAPTURES / "hdlc_9600.sr", 27e6)["D0"])
        self.assertEqual((p.code, p.stuff, p.sync), ("nrzi", (0, 5), [0, 1, 1, 1, 1, 1, 1, 0]))
        self.assertEqual((p.crc.width, p.crc.poly, p.crc.init, p.crc.xorout), (16, 0x1021, 1, 1))
        self.assertIsNotNone(crc_check(p))
        texts = [b"openPIO", b"hello", b"learns", b"any line", b"HDLC", b"from a recording"]
        got = [bytes(byte_lsb(f[k:k + 8]) for k in range(8, len(f) - 24, 8)) for f in p.frames]
        self.assertEqual(got, texts)

    def test_bridge_spi(self):
        """The Pico's SPI to the chip during bringup.py, sampled at 12 MHz: mode 0, framed by
        CS_N low, 1 MHz. In samples, and in clocks of 27 MHz, where a sample is 2.25 clocks and
        a data edge a sample before the clock's comes 2 or 3 clocks before it."""
        for hz in (12e6, 27e6):
            with self.subTest(hz=hz):
                c = Capture.from_sigrok(CAPTURES / "bridge_spi.sr", hz)
                p = infer_clocked({k: c[k] for k in ("D0", "D1", "D2")})[0]
                self.assertEqual((p.clock, p.data, p.select, p.cpol, p.cpha),
                                 ("D0", "D1", ("D2", 0), 0, 0))
                self.assertEqual(p.period, hz / 1e6)
                self.assertEqual(len(p.frames), 55)
                self.assertEqual([word_msb(p.frames[1][k:k + 16]) for k in (0, 16)],
                                 [0x4020, 0x0010])

    def test_nec_remote(self):
        """A NEC remote at a KY-022: ten keys, a repeat code after each and more on a held one
        (13 against 10 frames). Address 0; each byte's complement, a CRC-8 x^8 + 1."""
        p = infer_pulses(Capture.from_sigrok(CAPTURES / "nec_remote.sr", 27e6)["D0"])
        self.assertEqual((p.carrier, p.idle, p.one, p.ignored), ("space", 1, 1, 13))
        self.assertEqual((p.crc.width, p.crc.poly), (8, 0x01))
        self.assertIsNotNone(pulses_check(p))
        keys = [[byte_lsb(f[k:k + 8]) for k in (0, 8, 16, 24)] for f in p.frames]
        self.assertEqual([k[2] for k in keys], [0x45, 0x46, 0x47, 0x16, 0x0D, 0x1C, 0x08, 0x5A,
                                                0x19, 0x1C])
        self.assertTrue(all(a == 0 and b == 0xFF and c ^ d == 0xFF for a, b, c, d in keys))

    def test_ws2812(self):
        """ws2812.asm on pio 1 to a ring of 12 LEDs, random colours (each 0..23, G R B) every
        40 ms, sampled at 24 MHz (the times in samples): pulse width, idle low, a long mark a 1.
        The frames recorded are a run of the colours sent."""
        p = infer_pulses(Capture.from_sigrok(CAPTURES / "ws2812_random.sr", 24e6)["D0"])
        self.assertEqual((p.carrier, p.idle, p.one, p.crc), ("mark", 0, 1, None))
        self.assertEqual((round(p.short), round(p.long)), (9, 21))         # 375, 875 ns
        rnd = random.Random(5)
        sent = [[c for _ in range(12) for c in (rnd.randrange(24) for _ in range(3))]
                for _ in range(120)]
        sent = [[led[k] for i in range(0, 36, 3) for led in [f[i:i + 3]] for k in (1, 0, 2)]
                for f in sent]                                            # R G B -> G R B
        got = [[word_msb(f[k:k + 8]) for k in range(0, 288, 8)] for f in p.frames]
        start = sent.index(got[0])
        self.assertEqual(got, sent[start:start + len(got)])

    def test_ws2812_comet(self):
        """A comet going round the ring every 40 ms (DEMO.md): 23 frames, but only 12 kinds,
        each a rotation of the others by whole LEDs. No CRC: a CRC-15 that divides x^288 + 1
        fits every rotation of a frame it fits, and the rotations are no evidence."""
        p = infer_pulses(Capture.from_sigrok(CAPTURES / "ws2812_comet.sr", 24e6)["D0"])
        self.assertEqual((p.carrier, p.idle, p.one, p.crc), ("mark", 0, 1, None))
        self.assertEqual(len(p.frames), 23)
        leds = [[word_msb(f[k:k + 24]) for k in range(0, 288, 24)] for f in p.frames]
        tail = [0x010406, 0x02080C, 0x041018, 0x082030]           # G R B, the dimmest first
        for ring in leds:
            k = ring.index(tail[-1])
            self.assertEqual([ring[(k - 3 + i) % 12] for i in range(4)], tail)
            self.assertEqual(sum(1 for c in ring if c), 4)


    def test_midea_ac_remote(self):
        """A Comfee RG57H4 air conditioner remote (Midea) at the KY-022: seven presses, each a
        frame of 48 bits and 5.2 ms later the same inverted; a few marks broken by spikes of
        1 us. The last byte is 0x100 minus the sum of the others: a checksum, no CRC."""
        p = infer_pulses(Capture.from_sigrok(CAPTURES / "midea_ac_remote.sr", 27e6)["D0"])
        self.assertEqual((p.carrier, p.idle, p.one, p.crc, p.ignored), ("space", 1, 1, None, 0))
        self.assertEqual(len(p.frames), 14)
        for a, b in zip(p.frames[0::2], p.frames[1::2]):
            data = [byte_lsb(a[k:k + 8]) for k in range(0, 48, 8)]
            self.assertEqual(b, [x ^ 1 for x in a])
            self.assertEqual(data[0], 0x85)
            self.assertEqual(sum(data) & 0xFF, 0)


if __name__ == "__main__":
    unittest.main()
