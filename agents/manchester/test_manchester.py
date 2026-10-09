#!/usr/bin/env python3
"""
agents/manchester/test_manchester.py
Verification suite for Manchester / 10BASE-T Encoding.
Demonstrates the architectural limitation of pure firmware bit-banging versus PAU hardware assist.
"""

import sys
from pathlib import Path
import unittest

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from shared_tests.base_test import ProtocolTestCase

ASM_PATH = Path(__file__).resolve().parent / "manchester.asm"


class TestManchester(ProtocolTestCase):

    def setUp(self):
        super().setUp()
        self.load_assembly(str(ASM_PATH), set_base=0, out_base=0)
        self.sm.shift_out_right = False  # MSB first

    def test_manchester_firmware_overhead_failure(self):
        """Stress-test software Manchester serializer; detects inter-bit pipeline delay violation."""
        payload = 0xAA00  # Alternating 1s and 0s
        self.sm.tx_fifo.append(payload)

        pin_history = []
        for _ in range(80):
            pin_history.append(self.sm.gpio_out & 1)
            self.step(1)

            if self.sm.irq_flags[0]:
                break

        # Calculate maximum consecutive run of unchanged logic level
        max_run = 0
        current_run = 1
        for i in range(1, len(pin_history)):
            if pin_history[i] == pin_history[i - 1]:
                current_run += 1
                max_run = max(max_run, current_run)
            else:
                current_run = 1

        # Manchester specification mandates a transition at every bit center; no state may persist > 2 half-periods (6 cycles)
        if max_run > 6:
            self.dump_crash_log(f"Inter-bit instruction latency stretched level to {max_run} consecutive cycles")
            self.fail(f"ARCHITECTURAL LIMITATION: Software bit-banging produces {max_run}-cycle run (max allowed: 6). PAU Manchester TX hardware engine (PAU_CFG bit 3) is required to sustain 10BASE-T compliance.")

        self.print_success_summary("manchester_encoding")


if __name__ == "__main__":
    unittest.main()
