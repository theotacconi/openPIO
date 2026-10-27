#!/usr/bin/env python3
"""Two state machines as a protocol bridge: SM 0 receives UART bytes and pushes them straight
into SM 1's TX FIFO (push peer), SM 1 sends them as an SPI master. No host in between."""
import random
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "test" / "shared"))
sys.path.insert(0, str(ROOT / "sim" / "model"))
from bench import Bench                                  # noqa: E402
from devices import SpiSlave, UartTx                     # noqa: E402

RXD, MOSI, SCK, CS, MISO = 7, 0, 1, 2, 3


class UartToSpi(unittest.TestCase):

    def test_bridge(self):
        data = [random.Random(2).randrange(256) for _ in range(12)]
        bench = Bench(pullups=1 << RXD | 1 << CS)
        # The UART receiver of programs/uart, its pushes sent to the peer's TX FIFO instead.
        uart = (ROOT / "programs/uart/uart_rx.asm").read_text().replace("push noblock", "push peer noblock")
        bench.load(0, uart, in_base=RXD, clkdiv_int=2)
        bench.load(1, (ROOT / "programs/spi/spi_master.asm").read_text())   # mode 0
        uart_tx = bench.add(UartTx(RXD, 16, data, start=30))
        slave = bench.add(SpiSlave(SCK, MOSI, MISO, mode=0, cs=CS))
        bench.start(0b11)
        bench.run(uart_tx.end + 200)
        self.assertEqual(bench.errors(), [])
        self.assertEqual(slave.words, data)


if __name__ == "__main__":
    unittest.main()
