#!/usr/bin/env python3
"""
agents/uart/test_uart.py
Verification test bench for openPIO UART Receiver with Framing Check.
Simulates physical line interactions: idle high, start bit, LSB-first data bits, and stop bit validation.
"""

import sys
from pathlib import Path
import unittest

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from shared_tests.base_test import ProtocolTestCase

ASM_PATH = Path(__file__).resolve().parent / "uart.asm"


class TestUartRx(ProtocolTestCase):

    def setUp(self):
        super().setUp()
        self.load_assembly(str(ASM_PATH), in_base=0, jmp_pin=0)
        self.sm.gpio_in = 1  # Idle high
        self.step(1)         # Enter wait 0

    def _transmit_byte(self, byte_val: int, valid_stop: bool = True):
        """Simulate transmitting 1 start bit, 8 data bits (LSB-first), and 1 stop bit at 8 cycles/bit."""
        # Start bit: 0 for 8 cycles
        self.sm.gpio_in = 0
        self.step(8)

        # 8 Data bits: 8 cycles each
        for bit_idx in range(8):
            bit = (byte_val >> bit_idx) & 1
            self.sm.gpio_in = bit
            self.step(8)

        # Stop bit: 1 if valid, 0 if framing error (held for 16 cycles to allow check & push)
        self.sm.gpio_in = 1 if valid_stop else 0
        self.step(16)

        # Line returns to idle high
        self.sm.gpio_in = 1
        self.step(4)

    def test_valid_frame_reception(self):
        """Test standard 8N1 transmission of byte 0xA5."""
        expected_byte = 0xA5
        self._transmit_byte(expected_byte, valid_stop=True)

        self.assert_cycle(len(self.sm.rx_fifo) == 1, "RX FIFO should contain 1 byte")
        # In openPIO right-shift in: 8 bits shifted into 16-bit ISR -> byte is in upper 8 bits (0xA500)
        received_raw = self.sm.rx_fifo[0]
        received_byte = (received_raw >> 8) & 0xFF
        self.assertEqual(received_byte, expected_byte, f"Received 0x{received_byte:02x}, expected 0x{expected_byte:02x}")
        self.assertFalse(self.sm.irq_flags[0], "Framing error IRQ 0 should NOT be set")
        self.print_success_summary("uart_valid_frame")

    def test_framing_error_detection(self):
        """Test corrupted frame with stop bit missing (line stays low)."""
        self._transmit_byte(0x5A, valid_stop=False)

        self.assertTrue(self.sm.irq_flags[0], "Framing error IRQ 0 MUST be asserted on invalid stop bit")
        self.assertEqual(len(self.sm.rx_fifo), 0, "Corrupted frame must NOT be pushed to RX FIFO")
        self.print_success_summary("uart_framing_error")


if __name__ == "__main__":
    unittest.main()
