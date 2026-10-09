#!/usr/bin/env python3
"""
agents/ps2/test_ps2.py
Verification suite for PS/2 Keyboard/Mouse Protocol Receiver.
Simulates device-driven clock, 11-bit frame (Start, 8 Data, Odd Parity, Stop), and scancode decoding.
"""

import sys
from pathlib import Path
import unittest

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from shared_tests.base_test import ProtocolTestCase

ASM_PATH = Path(__file__).resolve().parent / "ps2.asm"


class TestPs2(ProtocolTestCase):

    def setUp(self):
        super().setUp()
        self.load_assembly(str(ASM_PATH), in_base=1)
        self.sm.shift_in_right = True  # LSB-first

    def _send_ps2_frame(self, scancode: int):
        """Simulate PS/2 device transmitting an 11-bit packet (Start=0, 8 Data LSB-first, Odd Parity, Stop=1)."""
        data_bits = [(scancode >> i) & 1 for i in range(8)]
        num_ones = sum(data_bits)
        parity_bit = 0 if (num_ones % 2 == 1) else 1  # Odd parity
        frame_bits = [0] + data_bits + [parity_bit] + [1]

        for bit in frame_bits:
            # 1. Setup DATA while CLK is High
            self.sm.gpio_in = (bit << 1) | 1
            self.step(3)

            # 2. Assert CLK Low (falling edge: openPIO samples DATA)
            self.sm.gpio_in = (bit << 1) | 0
            self.step(3)

            # 3. Return CLK High
            self.sm.gpio_in = (bit << 1) | 1
            self.step(3)

    def test_receive_scancode_key_a(self):
        """Verify reception of PS/2 scancode 0x1C (Key 'A')."""
        expected_scancode = 0x1C
        self._send_ps2_frame(expected_scancode)

        self.assert_cycle(len(self.sm.rx_fifo) == 1, "RX FIFO should receive 1 word")
        self.assertTrue(self.sm.irq_flags[0], "Frame Complete IRQ 0 must be asserted")

        raw_rx = self.sm.rx_fifo[0]
        # In 16-bit ISR with right-shift:
        # Parity bit is at bit 15.
        # Data bits 7:0 are at bits 14:7.
        extracted = 0
        for i in range(8):
            b = (raw_rx >> (14 - i)) & 1
            extracted |= (b << (7 - i))

        self.assertEqual(extracted, expected_scancode, f"Received scancode 0x{extracted:02x}, expected 0x{expected_scancode:02x}")
        self.print_success_summary("ps2_scancode")


if __name__ == "__main__":
    unittest.main()
