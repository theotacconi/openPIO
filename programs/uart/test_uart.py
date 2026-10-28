#!/usr/bin/env python3
"""UART transmitter and receiver judged by the reference UART devices: random bytes back to
back; TX at integer and fractional dividers (bit edges on time), RX with a transmitter 3 % fast
or slow and with framing errors. Full duplex on one SM through the PAU: both directions at
once, bit edges on time, a framing error. Autobaud: the rate learned from a leading 0x55, then
random bytes back to back, at bit times from 12 to 61 clocks that are not whole strobes."""
import random
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
from bench import Bench                                  # noqa: E402
from devices import UartRx, UartTx                       # noqa: E402


class UartTxTest(unittest.TestCase):

    def check(self, clkdiv_int, clkdiv_frac, max_skew):
        data = [random.Random(clkdiv_int).randrange(256) for _ in range(30)]
        bench = Bench(pullups=1)                         # UART lines idle high
        bench.load(0, (HERE / "uart_tx.asm").read_text(), clkdiv_int=clkdiv_int, clkdiv_frac=clkdiv_frac)
        bit = 8 * (clkdiv_int + clkdiv_frac / 256)
        rx = bench.add(UartRx(0, round(bit)))
        bench.send(0, data)
        bench.start(1)
        bench.run(int(bit * 10 * (len(data) + 2)))
        self.assertEqual(rx.frames, data)
        self.assertEqual(bench.errors(), [])
        self.assertLessEqual(rx.skew, max_skew)
        first, last = bench.edges(0)[0][0], bench.edges(0)[-1][0]
        self.assertAlmostEqual((last - first) / bit, 10 * len(data) - 1, delta=1,
                               msg="back to back: exactly 10 bits a byte")

    def test_integer_divider(self):
        self.check(3, 0, 0)

    def test_fractional_divider(self):
        self.check(2, 128, 1)                        # 20 clocks a bit on average: +-1 clock jitter


class UartRxTest(unittest.TestCase):

    def run_rx(self, clkdiv, data, gap=0.0, stop_low=frozenset(), tx_bit=None):
        bench = Bench(pullups=1 << 3)
        bench.load(0, (HERE / "uart_rx.asm").read_text(), clkdiv_int=clkdiv)
        tx = bench.add(UartTx(3, tx_bit or 8 * clkdiv, data, start=20, gap=gap, stop_low=stop_low))
        bench.start(1)
        bench.run(tx.end + 40)
        return bench, [w >> 8 for w in bench.received[0]]

    def test_back_to_back(self):
        data = [random.Random(7).randrange(256) for _ in range(30)]
        for clkdiv in (1, 4):
            with self.subTest(clkdiv=clkdiv):
                bench, got = self.run_rx(clkdiv, data)
                self.assertEqual(got, data)
                self.assertFalse(bench.chip.sms[0].irq_flags[0])

    def test_baud_tolerance(self):
        """A transmitter 3 % fast or slow: sampling in the middle of each bit still reads it."""
        data = [random.Random(9).randrange(256) for _ in range(20)]
        for tx_bit in (31, 33):                       # receiver: 32 clocks a bit
            with self.subTest(tx_bit=tx_bit):
                bench, got = self.run_rx(4, data, gap=0.5, tx_bit=tx_bit)
                self.assertEqual(got, data)
                self.assertFalse(bench.chip.sms[0].irq_flags[0])

    def test_framing_error(self):
        data = [0x11, 0x22, 0x33, 0x44]
        bench, got = self.run_rx(1, data, gap=1, stop_low={1})
        self.assertEqual(got, [0x11, 0x33, 0x44], "the frame with a low stop bit is dropped")
        self.assertTrue(bench.chip.sms[0].irq_flags[0])


class UartDuplexTest(unittest.TestCase):

    def run_duplex(self, out, into, stop_low=frozenset()):
        bench = Bench(pullups=0b11)
        bench.load(0, (HERE / "uart_duplex.asm").read_text(), clkdiv_int=2)   # 16 clocks a bit
        rx = bench.add(UartRx(0, 16))
        tx = bench.add(UartTx(1, 16, into, start=50, stop_low=stop_low))
        bench.send(0, out)
        bench.start(1)
        bench.run(max(tx.end, 16 * 11 * len(out)) + 400)
        return bench, rx

    def test_both_directions_at_once(self):
        rnd = random.Random(11)
        out, into = [rnd.randrange(256) for _ in range(40)], [rnd.randrange(256) for _ in range(40)]
        bench, rx = self.run_duplex(out, into)
        self.assertEqual(rx.frames, out)
        self.assertEqual(rx.skew, 0, "every TX edge on the bit grid")
        self.assertEqual([w >> 8 for w in bench.received[0]], into)
        self.assertEqual(bench.errors(), [])
        self.assertFalse(bench.chip.sms[0].irq_flags[0])

    def test_framing_error(self):
        bench, rx = self.run_duplex([0x55], [0x11, 0x22, 0x33], stop_low={1})
        self.assertTrue(bench.chip.sms[0].irq_flags[0])
        self.assertEqual(rx.frames, [0x55])


class UartAutobaudTest(unittest.TestCase):

    def test_rates(self):
        for bit in (12.0, 37.3, 60.7):
            with self.subTest(bit=bit):
                data = [random.Random(int(bit)).randrange(256) for _ in range(30)]
                bench = Bench(pullups=1)
                bench.load(0, (HERE / "uart_autobaud.asm").read_text())
                tx = bench.add(UartTx(0, bit, [0x55] + data, start=40))
                bench.start(1)
                bench.run(tx.end + round(2 * bit))
                words = bench.received[0]
                self.assertLessEqual(abs(words[0] - 8 * bit), 2, "8 bit times measured")
                self.assertEqual([w >> 8 for w in words[1:]], data)
                self.assertFalse(bench.chip.sms[0].irq_flags[0])


if __name__ == "__main__":
    unittest.main()
