#!/usr/bin/env python3
"""10BASE-T. Transmitter judged by a reference Manchester receiver: preamble and SFD, frame bits,
FCS against zlib.crc32, half-bit timing, TD- the complement of TD+, TP_IDL; at 40 MHz a 500-byte
frame loaded by an SPI-speed host into the 253-word TX FIFO (FIFO_SPLIT 31) before it starts,
at 60 MHz a 1518-byte frame streamed by that host while it is sent. Receiver at 60 MHz fed by a
reference Manchester transmitter: frames of 64 to 1518 bytes with their FCS verdicts, a
corrupted frame, a 1518-byte frame drained by an SPI-speed host through the 253-word RX FIFO
(FIFO_SPLIT 1), a transmitter 100 ppm fast or slow with its edges jittered by +-0.45 clock (an
unsynchronized source), and the chip receiving its own transmitter."""
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
from devices import ManchesterRx, ManchesterTx, bits_to_bytes  # noqa: E402

TDP, TDN, RDP = 0, 1, 0
SPI_WORD = 98             # clocks a host word takes: SCLK = clock / 6, bursts of 64 (97.5)


def frame_words(f):
    return [len(f) // 2] + [f[i] | f[i + 1] << 8 for i in range(0, len(f), 2)]


def line_bits(f):
    """Preamble, SFD, the frame and its FCS as sent, LSB first."""
    data = [0x55] * 7 + [0xD5] + list(f) + list(zlib.crc32(f).to_bytes(4, "little"))
    return [b >> k & 1 for b in data for k in range(8)]


def frames(rng, sizes):
    return [bytes(rng.randrange(256) for _ in range(n)) for n in sizes]


# ---------------------------------------------------------------------------------- transmitter

def tx_setup(baud=2, host_word_clocks=0):
    bench = Bench(host_word_clocks=host_word_clocks)
    bench.load(0, (HERE / "eth_tx.asm").read_text(), pau_cfg=baud)
    bench.write(0, IRQ_MASK, 1 << 1)              # HOST_IRQ only for "frame sent"
    rx = bench.add(ManchesterRx(TDP, 2 * baud))
    bench.start(1)
    return bench, rx


def send(bench, f, baud=2):
    """Host: load the frame (as much as the TX FIFO holds), set flag 0, wait for flag 1; the
    rest of the frame follows while it is sent."""
    sm = bench.chip.sms[0]
    bench.send(0, frame_words(f))
    bench.run(200 * len(f), until=lambda: not bench.to_send[0] or sm.tx_level == sm.tx_capacity)
    bench.write(0, IRQ, 1 << 8)
    bench.run((len(f) + 20) * 8 * 2 * baud, until=lambda: bench.chip.host_irq)
    bench.write(0, IRQ, 1 << 1)


class EthernetTx(unittest.TestCase):

    def check(self, f, bits):
        data = bits_to_bytes(bits)
        self.assertEqual(data[:8], [0x55] * 7 + [0xD5], "preamble, SFD")
        self.assertEqual(bytes(data[8:-4]), f)
        self.assertEqual(bytes(data[-4:]), zlib.crc32(f).to_bytes(4, "little"), "FCS")

    def test_frames(self):
        fs = frames(random.Random(4), (60, 64, 100))
        bench, rx = tx_setup()
        for f in fs:
            send(bench, f)
        bench.run(50)
        self.assertEqual(bench.errors(), [])
        self.assertEqual(len(rx.frames), len(fs))
        for f, bits in zip(fs, rx.frames):
            self.check(f, bits)
        tdp, tdn = bench.wave(TDP), bench.wave(TDN)
        self.assertTrue(all(n != p or p == "0" for p, n in zip(tdp, tdn)), "TD- = not TD+, or idle")
        self.assertIn("1" * 8 + "0", tdp, "TP_IDL: high for 2 bit times, then idle")

    def test_frame_from_a_slow_host(self):
        """An SPI host gives a word every ~98 clocks, the line at 40 MHz takes one every 64: a
        500-byte frame waits in the 253-word TX FIFO until the host sets flag 0."""
        f = frames(random.Random(5), (500,))[0]
        bench, rx = tx_setup(host_word_clocks=SPI_WORD)
        send(bench, f)
        self.assertFalse(bench.to_send[0], "the whole frame was in the FIFO")
        self.assertEqual(bench.errors(), [])
        self.check(f, rx.frames[0])

    def test_longest_frame_streamed_at_60_mhz(self):
        """At 60 MHz (3 strobes a half-bit) the line takes a word every 96 clocks, about as
        fast as the SPI host gives one: a 1518-byte frame starts with the FIFO full and the
        rest streams in while it is sent."""
        f = frames(random.Random(6), (1518,))[0]
        bench, rx = tx_setup(baud=3, host_word_clocks=SPI_WORD)
        send(bench, f, baud=3)
        self.assertEqual(bench.errors(), [])
        self.check(f, rx.frames[0])


# ---------------------------------------------------------------------------------- receiver

def rx_frames(words):
    """[(bits, verdict)] from the receiver's RX words."""
    out, bits, k = [], [], 0
    while k < len(words):
        w = words[k]
        if w & 1:                                    # the end word, then the verdict
            top = max(i for i in range(1, 16) if w >> i & 1)
            bits += [w >> i & 1 for i in range(1, top)]
            if k + 1 >= len(words):
                break
            out.append((bits, words[k + 1]))
            bits, k = [], k + 2
        else:
            bits += [w >> i & 1 for i in range(1, 16)]
            k += 1
    return out


class EthernetRx(unittest.TestCase):

    def receive(self, fs, bit=6, jitter=0, host_word_clocks=0, seed=0, bad=()):
        """The frames fs sent by a reference transmitter (frames in `bad` with one data bit
        flipped after the FCS was computed), received by eth_rx at 60 MHz."""
        bench = Bench(host_word_clocks=host_word_clocks)
        bench.load(0, (HERE / "eth_rx.asm").read_text())
        sent = []
        for i, f in enumerate(fs):
            bits = line_bits(f)
            if i in bad:
                bits[64 + 8 * (len(f) // 2) + 3] ^= 1
            sent.append(bits)
        tx = bench.add(ManchesterTx(RDP, bit, sent, start=20, jitter=jitter, seed=seed))
        bench.start(1)
        bench.run(tx.end + 2000 + 100 * len(fs[-1]),
                  until=lambda: len(rx_frames(bench.received[0])) == len(fs))
        self.assertEqual(bench.errors(), [])
        got = rx_frames(bench.received[0])
        self.assertEqual(len(got), len(fs))
        for i, (f, (bits, verdict)) in enumerate(zip(fs, got)):
            n = 8 * (len(f) + 4)
            self.assertIn(len(bits) - n, (0, 1, 2), "the frame, then at most 2 bits of TP_IDL")
            data = bytes(bits_to_bytes(bits[:n]))
            self.assertEqual(data[:-4], f if i not in bad else data[:-4])
            self.assertEqual(data[-4:], zlib.crc32(f).to_bytes(4, "little"), "FCS")
            self.assertEqual(verdict, 0 if i in bad else 1, f"frame {i}: the FCS verdict")
        return bench

    def test_frames(self):
        self.receive(frames(random.Random(7), (64, 100, 64, 333, 64)))

    def test_corrupted_frame(self):
        self.receive(frames(random.Random(8), (64, 200, 64)), bad=(1,))

    def test_longest_frame_to_a_slow_host(self):
        """The line gives a word every 90 clocks, the SPI host takes one every 98: a 1518-byte
        frame (812 words) leaves about 70 in the 253-word RX FIFO."""
        self.receive(frames(random.Random(9), (1518, 64)), host_word_clocks=SPI_WORD)

    def test_unsynchronized_source(self):
        for ppm, seed in ((100, 1), (-100, 2)):
            with self.subTest(ppm=ppm):
                self.receive(frames(random.Random(10 + seed), (64, 1518, 64)),
                             bit=6 * (1 + ppm * 1e-6), jitter=0.45, seed=seed)

    def test_own_transmitter(self):
        """eth_tx on SM 1 at 60 MHz drives pin 0, eth_rx on SM 0 receives it."""
        fs = frames(random.Random(11), (64, 300))
        bench = Bench()
        bench.load(0, (HERE / "eth_rx.asm").read_text())
        bench.load(1, (HERE / "eth_tx.asm").read_text(), pau_cfg=3)
        bench.write(1, IRQ_MASK, 1 << 1)
        bench.start(3)
        for f in fs:
            bench.send(1, frame_words(f))
            bench.run(200 * len(f), until=lambda: not bench.to_send[1])
            bench.write(1, IRQ, 1 << 8)
            bench.run((len(f) + 20) * 8 * 12, until=lambda: bench.chip.host_irq)
            bench.write(1, IRQ, 1 << 1)
        bench.run(500)
        self.assertEqual(bench.errors(), [])
        got = rx_frames(bench.received[0])
        self.assertEqual([bytes(bits_to_bytes(b))[:len(f)] for (b, _), f in zip(got, fs)], fs)
        self.assertEqual([v for _, v in got], [1] * len(fs))


if __name__ == "__main__":
    unittest.main()
