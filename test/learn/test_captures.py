"""test_captures.py - The learner on real recordings: board/captures/, a logic analyzer on the
Tang Nano 20K build (board/captures/README.md). What each one must be learned as, without a
board or sigrok."""

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
        """The Pico's SPI to the chip during bringup.py, sampled at 12 MHz (the times in
        samples): mode 0, framed by CS_N low, 1 MHz."""
        c = Capture.from_sigrok(CAPTURES / "bridge_spi.sr", 12e6)
        p = infer_clocked({k: c[k] for k in ("D0", "D1", "D2")})[0]
        self.assertEqual((p.clock, p.data, p.select, p.cpol, p.cpha), ("D0", "D1", ("D2", 0), 0, 0))
        self.assertEqual(p.period, 12)
        self.assertEqual(len(p.frames), 55)
        self.assertEqual([word_msb(p.frames[1][k:k + 16]) for k in (0, 16)], [0x4020, 0x0010])

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


if __name__ == "__main__":
    unittest.main()
