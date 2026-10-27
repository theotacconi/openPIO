#!/usr/bin/env python3
"""
agents/usb_ls/test_usb_ls.py
USB Low-Speed transmitter on the PAU: the D-/D+ waveform is decoded like a USB receiver
(NRZI, bit unstuffing, CRC16 against a software CRC-16/USB) and the EOP is timed.
"""

import sys
from pathlib import Path
import unittest

SHARED_DIR = Path(__file__).resolve().parents[2] / "test" / "shared"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))

from base_test import ProtocolTestCase

ASM_PATH = Path(__file__).resolve().parent / "usb_ls.asm"
BIT = 4                       # strobes per bit
J, K, SE0 = 1, 2, 0           # pins [1:0] = {D+, D-}


def crc16_usb(data: list[int]) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


class TestUsbLs(ProtocolTestCase):

    def setUp(self):
        super().setUp()
        self.load_assembly(str(ASM_PATH), set_base=0, out_base=0, set_count=2)

    def send(self, pid: int, payload: list[int]) -> list[int]:
        """Run one packet; return the pin states [1:0], one per strobe."""
        words = [0x80 | pid << 8, len(payload)] + payload
        line = []
        while not self.sm.irq_flags[0] and len(line) < 2000:
            if words and len(self.sm.tx_fifo) < self.sm.fifo_depth:   # the host keeps the FIFO fed
                self.sm.tx_fifo.append(words.pop(0))
            self.step(1)
            line.append(self.sm.pins & 3)
        self.assertTrue(self.sm.irq_flags[0], "packet never completed")
        return line

    def decode(self, line: list[int]) -> tuple[list[int], int, int]:
        """USB receiver: (unstuffed bits, stuffed bits removed, strobe where SE0 starts)."""
        start = line.index(K)                           # first SYNC bit is a 0: J -> K
        bits, state, ones, stuffed, t = [], J, 0, 0, start
        while line[t] != SE0:
            level = line[t + 1]                         # sample inside the bit
            self.assertIn(level, (J, K), f"illegal line state at strobe {t}")
            b = int(level == state)
            state = level
            if ones == 6:
                self.assertEqual(b, 0, "missing stuffed 0 after six 1s")
                ones, stuffed = 0, stuffed + 1
            else:
                bits.append(b)
                ones = ones + 1 if b else 0
            t += BIT
        return bits, stuffed, t

    def test_data_packet(self):
        """DATA0 with a payload that needs stuffing: SYNC, PID, payload and CRC16 decode back."""
        payload = [0x01, 0xFF, 0xFF, 0x7E, 0x00]
        line = self.send(0xC3, payload)
        bits, stuffed, se0 = self.decode(line)
        to_bytes = lambda b: [sum(b[i + k] << k for k in range(8)) for i in range(0, len(b) - 7, 8)]
        body = to_bytes(bits[:-16])
        crc = sum(b << k for k, b in enumerate(bits[-16:]))
        self.assertEqual(body, [0x80, 0xC3] + payload)
        self.assertEqual(crc, crc16_usb(payload), "CRC16 differs from CRC-16/USB")
        self.assertGreater(stuffed, 0, "payload with 0xFF must carry stuffed bits")
        # EOP: SE0 for exactly 2 bit times right after the last bit, then J for one bit time
        self.assertEqual(line[se0:se0 + 2 * BIT], [SE0] * (2 * BIT), "SE0 must last 2 bit times")
        self.assertEqual(line[se0 + 2 * BIT:se0 + 3 * BIT], [J] * BIT, "J must follow for 1 bit time")
        self.print_success_summary("usb_ls_data_packet")

    def test_empty_payload(self):
        """Zero-length DATA1: SYNC, PID, CRC16 of nothing (0x0000)."""
        bits, _, _ = self.decode(self.send(0x4B, []))
        self.assertEqual(len(bits), 32)
        self.assertEqual(sum(b << k for k, b in enumerate(bits[16:])), crc16_usb([]))
        self.print_success_summary("usb_ls_empty_packet")


if __name__ == "__main__":
    unittest.main()
