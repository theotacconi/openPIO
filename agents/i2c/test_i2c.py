#!/usr/bin/env python3
"""
agents/i2c/test_i2c.py
Verification suite for I2C Master with Clock Stretching and Multi-Master Arbitration.
Simulates open-drain bus, external pull-ups, slave clock stretching, and competing master collision.
"""

import sys
from pathlib import Path
import unittest

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from shared_tests.base_test import ProtocolTestCase

ASM_PATH = Path(__file__).resolve().parent / "i2c.asm"


class TestI2cMaster(ProtocolTestCase):

    def setUp(self):
        super().setUp()
        self.load_assembly(str(ASM_PATH), set_base=0, out_base=0, in_base=0, jmp_pin=1)
        self.sm.shift_out_right = False  # MSB first
        self.sm.shift_in_right = False

    def test_nominal_transfer_with_ack(self):
        """Test nominal byte transmission with successful ACK and STOP condition."""
        tx_byte = 0x94  # Address 0x4A + Write
        self.sm.tx_fifo.append(tx_byte << 8)

        # Run until push
        for _ in range(100):
            # Physical open-drain resolution: line is 0 if driven by SM, else 1 via pull-up
            scl = 0 if (self.sm.pindirs & 1) else 1
            sda = 0 if (self.sm.pindirs & 2) else 1
            self.sm.gpio_in = (sda << 1) | scl
            self.step(1)

            if len(self.sm.rx_fifo) > 0:
                break

        self.assert_cycle(len(self.sm.rx_fifo) == 1, "Transfer should complete and push ACK to FIFO")
        self.assertFalse(self.sm.irq_flags[1], "Arbitration error IRQ 1 should NOT be set")
        self.print_success_summary("i2c_nominal")

    def test_clock_stretching(self):
        """Simulate slow slave holding SCL low for 10 cycles; verify Master pauses and resumes cleanly."""
        tx_byte = 0xA0
        self.sm.tx_fifo.append(tx_byte << 8)

        for c in range(120):
            slave_stretch = (15 <= c <= 25)
            scl = 0 if ((self.sm.pindirs & 1) or slave_stretch) else 1
            sda = 0 if (self.sm.pindirs & 2) else 1
            self.sm.gpio_in = (sda << 1) | scl
            self.step(1)

            if len(self.sm.rx_fifo) > 0:
                break

        self.assert_cycle(len(self.sm.rx_fifo) == 1, "Master must resume after clock stretch and finish")
        self.print_success_summary("i2c_clock_stretching")

    def test_multi_master_arbitration_loss(self):
        """Simulate competing master driving SDA low while local master transmits '1'; verify collision abort."""
        tx_byte = 0x80  # MSB is 1
        self.sm.tx_fifo.append(tx_byte << 8)

        collision_detected = False
        for c in range(50):
            scl = 0 if (self.sm.pindirs & 1) else 1
            # Competing master asserts SDA dominant (0) during transmission of bit 1
            peer_driving_zero = (c >= 8)
            sda = 0 if ((self.sm.pindirs & 2) or peer_driving_zero) else 1
            self.sm.gpio_in = (sda << 1) | scl
            self.step(1)

            if self.sm.irq_flags[1]:
                collision_detected = True
                break

        self.assertTrue(collision_detected, "Master MUST assert IRQ 1 upon losing arbitration")
        # Step once more to execute abort handler
        self.step(2)
        self.assertEqual(self.sm.pindirs, 0, "Master MUST completely release bus (pindirs=0) after arbitration loss")
        self.print_success_summary("i2c_arbitration_loss")


if __name__ == "__main__":
    unittest.main()
