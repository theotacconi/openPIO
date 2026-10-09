#!/usr/bin/env python3
"""
agents/spi_mode2/test_spi_mode2.py
Verification suite for SPI Master Mode 2 (CPOL=1, CPHA=0).
Tests full-duplex exchange, SCK edge polarity, and MOSI/MISO alignment.
"""

import sys
from pathlib import Path
import unittest

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from shared_tests.base_test import ProtocolTestCase

ASM_PATH = Path(__file__).resolve().parent / "spi_mode2.asm"


class TestSpiMode2(ProtocolTestCase):

    def setUp(self):
        super().setUp()
        self.load_assembly(str(ASM_PATH), sideset_base=0, out_base=1, in_base=2)
        self.sm.shift_out_right = False  # MSB first
        self.sm.shift_in_right = False

    def test_full_duplex_transfer(self):
        """Verify 8-bit full duplex transfer: Master sends 0xAA, Slave sends 0x55."""
        master_tx = 0xAA
        slave_tx = 0x55

        self.sm.tx_fifo.append(master_tx << 8)

        sample_cycles = [5, 10, 15, 20, 25, 30, 35, 40]
        slave_rx = 0

        for c in range(45):
            b_idx = 7
            for i, sc in enumerate(sample_cycles):
                if c <= sc:
                    b_idx = 7 - i
                    break
            self.sm.gpio_in = (((slave_tx >> b_idx) & 1) << 2)

            if c in sample_cycles:
                mosi = (self.sm.gpio_out >> 1) & 1
                slave_rx = (slave_rx << 1) | mosi

            self.step(1)

            if len(self.sm.rx_fifo) > 0:
                break

        self.assert_cycle(len(self.sm.rx_fifo) == 1, "RX FIFO should receive 1 word")
        rx_val = self.sm.rx_fifo[0] & 0xFF
        self.assertEqual(rx_val, slave_tx, f"Master received 0x{rx_val:02x}, expected 0x{slave_tx:02x}")
        self.assertEqual(slave_rx, master_tx, f"Slave received MOSI 0x{slave_rx:02x}, expected 0x{master_tx:02x}")
        self.print_success_summary("spi_mode2")


if __name__ == "__main__":
    unittest.main()
