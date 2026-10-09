#!/usr/bin/env python3
"""
agents/spi_mode1/test_spi_mode1.py
Verification suite for SPI Master Mode 1 (CPOL=0, CPHA=1).
Tests full-duplex exchange, SCK edge polarity, and MOSI/MISO alignment.
"""

import sys
from pathlib import Path
import unittest

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from shared_tests.base_test import ProtocolTestCase

ASM_PATH = Path(__file__).resolve().parent / "spi_mode1.asm"


class TestSpiMode1(ProtocolTestCase):

    def setUp(self):
        super().setUp()
        self.load_assembly(str(ASM_PATH), sideset_base=0, out_base=1, in_base=2)
        self.sm.shift_out_right = False  # MSB first
        self.sm.shift_in_right = False

    def test_full_duplex_transfer(self):
        """Verify 8-bit full duplex transfer: Master sends 0x5A, Slave sends 0xC3."""
        master_tx = 0x5A
        slave_tx = 0xC3

        self.sm.tx_fifo.append(master_tx << 8)

        captured_mosi = 0
        bit_idx = 7
        prev_sck = 0

        for _ in range(50):
            curr_sck = self.sm.gpio_out & 1
            curr_mosi = (self.sm.gpio_out >> 1) & 1

            # Rising edge of SCK: Master drives MOSI, Slave prepares MISO
            if prev_sck == 0 and curr_sck == 1:
                if bit_idx >= 0:
                    miso_bit = (slave_tx >> bit_idx) & 1
                    self.sm.gpio_in = (miso_bit << 2)

            # Falling edge of SCK: Master samples MISO, Slave samples MOSI
            if prev_sck == 1 and curr_sck == 0:
                captured_mosi = (captured_mosi << 1) | curr_mosi
                bit_idx -= 1

            prev_sck = curr_sck
            self.step(1)

            if len(self.sm.rx_fifo) > 0:
                break

        self.assert_cycle(len(self.sm.rx_fifo) == 1, "RX FIFO should receive 1 word")
        rx_val = self.sm.rx_fifo[0] & 0xFF
        self.assertEqual(rx_val, slave_tx, f"Master received 0x{rx_val:02x}, expected 0x{slave_tx:02x}")
        self.assertEqual(captured_mosi, master_tx, f"Slave received MOSI 0x{captured_mosi:02x}, expected 0x{master_tx:02x}")
        self.print_success_summary("spi_mode1")


if __name__ == "__main__":
    unittest.main()
