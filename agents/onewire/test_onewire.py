#!/usr/bin/env python3
"""
agents/onewire/test_onewire.py
Verification suite for 1-Wire Master Protocol.
Simulates open-drain DQ bus, pull-up resistor, slave presence pulse, and read/write timeslots.
"""

import sys
from pathlib import Path
import unittest

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from shared_tests.base_test import ProtocolTestCase

ASM_PATH = Path(__file__).resolve().parent / "onewire.asm"


class TestOneWire(ProtocolTestCase):

    def setUp(self):
        super().setUp()
        self.load_assembly(str(ASM_PATH), set_base=0, out_base=0, in_base=0, jmp_pin=0)
        self.sm.shift_out_right = True  # 1-Wire is standard LSB-first
        self.sm.shift_in_right = True

    def test_presence_pulse_detection_and_read(self):
        """Simulate slave generating presence pulse and responding with 0x55 on read slots."""
        # Enqueue 0xFF to issue 8 read slots
        self.sm.tx_fifo.append(0xFF)

        slave_response = 0x55
        read_slot_index = 0

        for c in range(160):
            # Slave pulls DQ low for presence pulse between cycles 27 and 35
            slave_presence = (27 <= c <= 35)

            # During read slots (master drives low briefly, then releases), slave pulls low if bit is 0
            # Check if master is driving line low
            master_driving_low = (self.sm.pindirs & 1) != 0

            # Slave responds to read slots after cycle 45
            slave_driving_data = False
            if c > 45 and not master_driving_low and read_slot_index < 8:
                # Slave drives 0 if bit is 0
                expected_bit = (slave_response >> min(7, read_slot_index)) & 1
                if expected_bit == 0:
                    slave_driving_data = True

            dq = 0 if (master_driving_low or slave_presence or slave_driving_data) else 1
            self.sm.gpio_in = dq
            self.step(1)

            # Advance read slot index when master initiates a new slot (pindirs goes from 0 to 1)
            if c > 45 and self.sm.pindirs & 1:
                if (c % 10) == 0:  # Slot pacing
                    read_slot_index += 1

            if len(self.sm.rx_fifo) > 0:
                break

        self.assertTrue(self.sm.irq_flags[0], "Presence pulse IRQ 0 must be asserted")
        self.assertFalse(self.sm.irq_flags[1], "No-presence error IRQ 1 must NOT be asserted")
        self.assert_cycle(len(self.sm.rx_fifo) == 1, "RX FIFO should receive 1 byte")
        self.print_success_summary("onewire_presence")

    def test_no_presence_detection(self):
        """Simulate bus with no slave device connected (pull-up keeps line high)."""
        self.sm.tx_fifo.append(0xFF)

        for _ in range(40):
            dq = 0 if (self.sm.pindirs & 1) else 1
            self.sm.gpio_in = dq
            self.step(1)

            if self.sm.irq_flags[1]:
                break

        self.assertTrue(self.sm.irq_flags[1], "No-presence error IRQ 1 MUST be asserted when line remains high")
        self.assertFalse(self.sm.irq_flags[0], "Presence IRQ 0 must NOT be asserted")
        self.assertEqual(len(self.sm.rx_fifo), 0, "No data should be pushed when slave is absent")
        self.print_success_summary("onewire_no_presence")


if __name__ == "__main__":
    unittest.main()
