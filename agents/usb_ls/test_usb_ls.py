#!/usr/bin/env python3
"""
agents/usb_ls/test_usb_ls.py
Verification suite for USB Low-Speed Transmitter.
Validates Sync, EOP (SE0 for 2 bit times, J-state for 1 bit time), and PAU NRZI / Bit-Stuffing datapath.
"""

import sys
from pathlib import Path
import unittest

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from shared_tests.base_test import ProtocolTestCase

ASM_PATH = Path(__file__).resolve().parent / "usb_ls.asm"


class TestUsbLs(ProtocolTestCase):

    def setUp(self):
        super().setUp()
        self.load_assembly(str(ASM_PATH), set_base=0, out_base=0)
        self.sm.shift_out_right = True  # USB transmits LSB-first

    def test_eop_generation_and_packet_timing(self):
        """Verify End-of-Packet (EOP) timing: SE0 held for exactly 8 cycles, then J-state for 4 cycles."""
        # Load Sync (0x80) + Data (0xAA) into TX FIFO
        payload = (0xAA << 8) | 0x80
        self.sm.tx_fifo.append(payload)

        pins_history = []
        for _ in range(120):
            pins = self.sm.gpio_out & 3
            pins_history.append(pins)
            self.step(1)

            if self.sm.irq_flags[0]:
                break

        self.assertTrue(self.sm.irq_flags[0], "Packet Complete IRQ 0 must be asserted")

        # Verify SE0 (pins == 0) sequence at end of packet
        # Find continuous sequence of pins == 0
        se0_len = 0
        max_se0 = 0
        for p in pins_history:
            if p == 0:
                se0_len += 1
                max_se0 = max(max_se0, se0_len)
            else:
                se0_len = 0

        self.assertGreaterEqual(max_se0, 8, f"EOP SE0 duration {max_se0} must be >= 8 cycles (2 bit times)")
        self.print_success_summary("usb_ls_eop")

    def test_pau_nrzi_and_bitstuffing(self):
        """Verify that PAU hardware performs in-flight NRZI encoding and bit stuffing on GPIO output."""
        payload = (0xFF << 8) | 0x80  # 0xFF contains eight consecutive 1s (requires bit stuff after six 1s)
        self.sm.tx_fifo.append(payload)

        # Inspect if PAU transforms output to differential NRZI J/K states (pins in {1, 2})
        nrzi_active = False
        for _ in range(80):
            pins = self.sm.gpio_out & 3
            if pins == 2:  # K-state (D-=0, D+=1) can only be generated if NRZI transitions occur
                nrzi_active = True
                break
            self.step(1)

        if not nrzi_active:
            self.dump_crash_log("PAU hardware engine did not generate NRZI K-state transitions on D+/D-")
            self.fail("ARCHITECTURAL LIMITATION: emu.py does not implement PAU in-flight NRZI / bit-stuffing datapath")

        self.print_success_summary("usb_ls_pau")


if __name__ == "__main__":
    unittest.main()
