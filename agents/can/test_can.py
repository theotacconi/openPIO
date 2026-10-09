#!/usr/bin/env python3
"""
agents/can/test_can.py
Verification suite for CAN Bit Arbitration and Collision Detection.
Simulates dominant (0) vs recessive (1) bus dynamics and multi-node arbitration.
"""

import sys
from pathlib import Path
import unittest

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from shared_tests.base_test import ProtocolTestCase

ASM_PATH = Path(__file__).resolve().parent / "can.asm"


class TestCanController(ProtocolTestCase):

    def setUp(self):
        super().setUp()
        self.load_assembly(str(ASM_PATH), set_base=0, out_base=0, in_base=0, jmp_pin=1)
        self.sm.shift_out_right = False  # MSB first
        self.sm.shift_in_right = False

    def test_uncontested_arbitration_success(self):
        """Test transmission of 11-bit ID 0x5A5 with no bus conflict; verify arbitration won."""
        identifier = 0b10110100101  # 0x5A5
        self.sm.tx_fifo.append(identifier << 5)

        for _ in range(100):
            # Loopback: bus RX equals node TX
            tx = self.sm.gpio_out & 1
            self.sm.gpio_in = (tx << 1)
            self.step(1)

            if self.sm.irq_flags[0]:
                break

        self.assertTrue(self.sm.irq_flags[0], "Arbitration Won IRQ 0 must be asserted")
        self.assertFalse(self.sm.irq_flags[1], "Collision IRQ 1 must NOT be asserted")
        self.print_success_summary("can_uncontested")

    def test_arbitration_collision_loss(self):
        """Test collision where higher-priority node drives Dominant (0) while local node sends Recessive (1)."""
        identifier = 0b10110100101  # 0x5A5
        self.sm.tx_fifo.append(identifier << 5)

        for c in range(100):
            tx = self.sm.gpio_out & 1
            # Competing node forces bus dominant (0) starting at cycle 16
            bus_state = 0 if (c >= 16) else tx
            self.sm.gpio_in = (bus_state << 1)
            self.step(1)

            if self.sm.irq_flags[1]:
                break

        self.assertTrue(self.sm.irq_flags[1], "Collision IRQ 1 MUST be asserted upon losing arbitration")
        self.assertFalse(self.sm.irq_flags[0], "Arbitration Won IRQ 0 must NOT be asserted")
        # Ensure node ceased driving and reverted to recessive 1
        self.assertEqual(self.sm.gpio_out & 1, 1, "TX must immediately release to recessive 1")
        self.print_success_summary("can_arbitration_loss")


if __name__ == "__main__":
    unittest.main()
