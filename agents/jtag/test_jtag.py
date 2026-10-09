#!/usr/bin/env python3
"""
agents/jtag/test_jtag.py
Verification suite for IEEE 1149.1 JTAG TAP State Machine Transitions.
Models standard 16-state TAP FSM and validates state navigation from Reset -> Shift-DR -> Update-DR -> Idle.
"""

import sys
from pathlib import Path
import unittest

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from shared_tests.base_test import ProtocolTestCase

ASM_PATH = Path(__file__).resolve().parent / "jtag.asm"


class JtagTapFsm:
    """Cycle-accurate model of IEEE 1149.1 Standard TAP State Machine."""

    def __init__(self):
        self.state = "RESET"

    def step(self, tms: int) -> str:
        s = self.state
        if s == "RESET":
            self.state = "RESET" if tms else "IDLE"
        elif s == "IDLE":
            self.state = "SELECT_DR" if tms else "IDLE"
        elif s == "SELECT_DR":
            self.state = "SELECT_IR" if tms else "CAPTURE_DR"
        elif s == "CAPTURE_DR":
            self.state = "EXIT1_DR" if tms else "SHIFT_DR"
        elif s == "SHIFT_DR":
            self.state = "EXIT1_DR" if tms else "SHIFT_DR"
        elif s == "EXIT1_DR":
            self.state = "UPDATE_DR" if tms else "PAUSE_DR"
        elif s == "PAUSE_DR":
            self.state = "EXIT2_DR" if tms else "PAUSE_DR"
        elif s == "EXIT2_DR":
            self.state = "UPDATE_DR" if tms else "SHIFT_DR"
        elif s == "UPDATE_DR":
            self.state = "SELECT_DR" if tms else "IDLE"
        return self.state


class TestJtagTap(ProtocolTestCase):

    def setUp(self):
        super().setUp()
        self.load_assembly(str(ASM_PATH), sideset_base=0, set_base=0, set_count=3, out_base=2, in_base=3)
        self.sm.shift_out_right = True  # JTAG scans LSB-first
        self.sm.shift_in_right = True

    def test_tap_state_machine_navigation(self):
        """Verify that openPIO navigates standard TAP states: Reset -> Idle -> Shift-DR -> Exit1 -> Update-DR -> Idle."""
        tdi_payload = 0xA5
        self.sm.tx_fifo.append(tdi_payload)

        tap = JtagTapFsm()
        prev_tck = 0
        visited_states = []

        for _ in range(120):
            tck = self.sm.gpio_out & 1
            tms = (self.sm.gpio_out >> 1) & 1

            # Transition on rising edge of TCK
            if prev_tck == 0 and tck == 1:
                st = tap.step(tms)
                visited_states.append(st)

            prev_tck = tck
            self.step(1)

            if self.sm.irq_flags[0]:
                break

        self.assertTrue(self.sm.irq_flags[0], "TAP Sequence IRQ 0 must be asserted")
        self.assertIn("SHIFT_DR", visited_states, "FSM must enter SHIFT_DR state")
        self.assertIn("UPDATE_DR", visited_states, "FSM must enter UPDATE_DR state")
        self.assertEqual(tap.state, "IDLE", "FSM must safely park in RUN-TEST/IDLE state")
        self.print_success_summary("jtag_tap_fsm")


if __name__ == "__main__":
    unittest.main()
