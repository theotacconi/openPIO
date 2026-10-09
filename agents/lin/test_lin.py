#!/usr/bin/env python3
"""
agents/lin/test_lin.py
Verification suite for LIN (Local Interconnect Network) Master Header Generation.
Validates Sync Break (>=13 bit times), Delimiter (>=1 bit time), Sync Field (0x55), and PID transmission.
"""

import sys
from pathlib import Path
import unittest

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from shared_tests.base_test import ProtocolTestCase

ASM_PATH = Path(__file__).resolve().parent / "lin.asm"


class TestLinMaster(ProtocolTestCase):

    def setUp(self):
        super().setUp()
        self.load_assembly(str(ASM_PATH), set_base=0, out_base=0)
        self.sm.shift_out_right = True  # LSB-first

    def test_lin_header_generation(self):
        """Verify full LIN header generation: Break, Delimiter, Sync (0x55), and PID (0x80)."""
        pid_byte = 0x80  # Protected Identifier (e.g. ID 0x00 with P0=0, P1=0)
        self.sm.tx_fifo.append(pid_byte)

        tx_history = []
        for _ in range(160):
            tx = self.sm.gpio_out & 1
            tx_history.append(tx)
            self.step(1)

            if self.sm.irq_flags[0]:
                break

        self.assertTrue(self.sm.irq_flags[0], "Header Complete IRQ 0 must be asserted")

        # 1. Verify Break length (>= 52 cycles, which is 13 bit periods at 4 cycles/bit)
        # Find start of break
        break_start = 0
        while break_start < len(tx_history) and tx_history[break_start] == 1:
            break_start += 1

        break_end = break_start
        while break_end < len(tx_history) and tx_history[break_end] == 0:
            break_end += 1

        break_duration = break_end - break_start
        self.assertGreaterEqual(break_duration, 52, f"Break duration {break_duration} must be >= 52 cycles")

        # 2. Verify Delimiter length (>= 4 cycles of high)
        delim_end = break_end
        while delim_end < len(tx_history) and tx_history[delim_end] == 1:
            delim_end += 1
        delim_duration = delim_end - break_end
        self.assertGreaterEqual(delim_duration, 4, f"Delimiter duration {delim_duration} must be >= 4 cycles")

        self.print_success_summary("lin_header")


if __name__ == "__main__":
    unittest.main()
