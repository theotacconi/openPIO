#!/usr/bin/env python3
"""
agents/manchester/test_manchester.py
Manchester transmitter on the PAU: the pin must carry the exact IEEE 802.3 waveform of every
pushed word, MSB first, with no gap between words (one instruction, autopull).
"""

import sys
from pathlib import Path
import unittest

SHARED_DIR = Path(__file__).resolve().parents[2] / "test" / "shared"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))

from base_test import ProtocolTestCase

ASM_PATH = Path(__file__).resolve().parent / "manchester.asm"
HALF = 3


class TestManchester(ProtocolTestCase):

    def setUp(self):
        super().setUp()
        self.load_assembly(str(ASM_PATH), set_base=0, out_base=0)
        self.sm.shift_out_right = False  # MSB first

    def test_gapless_stream(self):
        words = [0xA5F0, 0x1234, 0xFFFF, 0x0001]
        pending = list(words) * 2                     # 8 words through a 4-word FIFO
        line = []
        for _ in range(16 * 2 * HALF * len(pending) + 40):
            if pending and len(self.sm.tx_fifo) < self.sm.fifo_depth:
                self.sm.tx_fifo.append(pending.pop(0))
            self.step(1)
            line.append(self.sm.pins & 1)
        words = words * 2
        expected = []
        for w in words:
            for i in range(15, -1, -1):
                b = (w >> i) & 1
                expected += [1 - b] * HALF + [b] * HALF
        text = "".join(map(str, line))
        self.assertIn("".join(map(str, expected)), text, "waveform differs from IEEE 802.3 Manchester")
        self.print_success_summary("manchester_stream")


if __name__ == "__main__":
    unittest.main()
