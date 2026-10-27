#!/usr/bin/env python3
"""10BASE-T transmitter judged by a reference Manchester receiver: preamble and SFD, frame bits,
FCS against zlib.crc32, half-bit timing, TD- the complement of TD+, TP_IDL; a 250-byte frame
loaded by an SPI-speed host into the 133-word FIFO, then started by a flag the host sets."""
import random
import sys
import unittest
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
sys.path.insert(0, str(HERE.parents[1] / "sim" / "model"))
from bench import Bench                                  # noqa: E402
from chip import IRQ, IRQ_MASK                           # noqa: E402
from devices import ManchesterRx, bits_to_bytes          # noqa: E402

TDP, TDN, BIT = 0, 1, 4


def frame_words(f):
    return [len(f) // 2] + [f[i] | f[i + 1] << 8 for i in range(0, len(f), 2)]


def setup(host_word_clocks=0):
    bench = Bench(host_word_clocks=host_word_clocks)
    bench.load(0, (HERE / "eth_tx.asm").read_text())
    bench.chip.reg_write(0, IRQ_MASK, 1 << 1)              # HOST_IRQ only for "frame sent"
    rx = bench.add(ManchesterRx(TDP, BIT))
    bench.start(1)
    return bench, rx


def send(bench, f):
    """Host: load the frame, set flag 0, wait for flag 1."""
    bench.send(0, frame_words(f))
    bench.run(200 * len(f), until=lambda: not bench.to_send[0])
    bench.chip.reg_write(0, IRQ, 1 << 8)
    bench.run((len(f) + 20) * 8 * BIT, until=lambda: bench.chip.host_irq)
    bench.chip.reg_write(0, IRQ, 1 << 1)


class EthernetTx(unittest.TestCase):

    def check(self, f, bits):
        data = bits_to_bytes(bits)
        self.assertEqual(data[:8], [0x55] * 7 + [0xD5], "preamble, SFD")
        self.assertEqual(bytes(data[8:-4]), f)
        self.assertEqual(bytes(data[-4:]), zlib.crc32(f).to_bytes(4, "little"), "FCS")

    def test_frames(self):
        rng = random.Random(4)
        frames = [bytes(rng.randrange(256) for _ in range(n)) for n in (60, 64, 100)]
        bench, rx = setup()
        for f in frames:
            send(bench, f)
        bench.run(50)
        self.assertEqual(bench.errors(), [])
        self.assertEqual(len(rx.frames), len(frames))
        for f, bits in zip(frames, rx.frames):
            self.check(f, bits)
        tdp, tdn = bench.wave(TDP), bench.wave(TDN)
        self.assertTrue(all(n != p or p == "0" for p, n in zip(tdp, tdn)), "TD- = not TD+, or idle")
        self.assertIn("1" * 8 + "0", tdp, "TP_IDL: high for 2 bit times, then idle")

    def test_frame_from_a_slow_host(self):
        """An SPI host gives a word every ~96 clocks, the line takes one every 64: the frame waits
        in the 133-word FIFO (data SRAM) until the host sets flag 0."""
        f = bytes(random.Random(5).randrange(256) for _ in range(250))
        bench, rx = setup(host_word_clocks=96)
        send(bench, f)
        self.assertEqual(bench.errors(), [])
        self.check(f, rx.frames[0])


if __name__ == "__main__":
    unittest.main()
