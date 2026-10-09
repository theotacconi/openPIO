#!/usr/bin/env python3
"""
agents/swd/test_swd.py
Verification suite for ARM Serial Wire Debug (SWD) Host Protocol.
Simulates bidirectional SWDIO line, turnaround (Trn) cycle line float, and Target ACK OK response.
"""

import sys
from pathlib import Path
import unittest

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from shared_tests.base_test import ProtocolTestCase

ASM_PATH = Path(__file__).resolve().parent / "swd.asm"


class TestSwdHost(ProtocolTestCase):

    def setUp(self):
        super().setUp()
        self.load_assembly(str(ASM_PATH), sideset_base=0, out_base=1, in_base=1, set_base=0)
        self.sm.shift_out_right = True  # SWD is LSB-first
        self.sm.shift_in_right = True

    def test_swd_request_and_ack_ok(self):
        """Verify 8-bit Host request transmission, turnaround line release, and Target ACK OK (001b)."""
        request_byte = 0xA5  # AP read request
        self.sm.tx_fifo.append(request_byte)

        # Target responds with ACK OK (0b001 -> bits 1, 0, 0)
        # Sample cycles in SWD assembly occur at cycles 50, 55, 60
        trn_observed = False

        for c in range(70):
            # Verify Turnaround (Trn) cycle: host must release SWDIO (pindirs bit 1 == 0)
            if 42 <= c <= 46:
                if (self.sm.pindirs & 2) == 0:
                    trn_observed = True

            # Target drives SWDIO during ACK phase
            if 48 <= c < 53:
                self.sm.gpio_in = (1 << 1)  # Bit 0 = 1
            elif 53 <= c < 63:
                self.sm.gpio_in = (0 << 1)  # Bits 1 & 2 = 0
            else:
                self.sm.gpio_in = 0

            self.step(1)

            if len(self.sm.rx_fifo) > 0:
                break

        self.assertTrue(trn_observed, "SWDIO line must be released (pindirs bit 1 = 0) during Turnaround cycle")
        self.assert_cycle(len(self.sm.rx_fifo) == 1, "RX FIFO should receive ACK word")
        raw_rx = self.sm.rx_fifo[0]
        # In 16-bit ISR with right-shift: 3 bits reside at bits [15:13]
        ack_code = (raw_rx >> 13) & 7
        self.assertEqual(ack_code, 1, f"Expected ACK OK (1), received {ack_code}")
        self.print_success_summary("swd_trn_ack")


if __name__ == "__main__":
    unittest.main()
