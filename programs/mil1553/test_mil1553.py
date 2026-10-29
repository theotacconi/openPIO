#!/usr/bin/env python3
"""MIL-STD-1553B bus controller against a reference remote terminal behind a bus transceiver,
1 Mbit/s Manchester II from a 50 MHz clock: a receive command with its data words back to back
and the terminal's status word; a transmit command answered by the status word and data words,
one of them with a wrong parity (flagged); response times of 4 and 12 us; a command to an
absent terminal (no response by 14 us). The terminal checks every word's sync, each mid-bit
transition within 2 clocks (40 ns) of its place, TX against TXB, odd parity, and that nothing
drives the bus while the other does. A protocol written after the ISA was frozen (2026-10-28),
without changing the hardware."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
sys.path.insert(0, str(HERE.parents[1] / "sim" / "model"))
from bench import Bench                                  # noqa: E402
from devices import Mil1553Rt                            # noqa: E402

TX, TXB, INH, RX = 0, 1, 2, 3                            # pins as in the program
RT = 5
PARITY_OK = 1 << 12                                      # in the STATUS word after each word


def command(address: int, transmit: int, subaddress: int, count: int) -> int:
    return address << 11 | transmit << 10 | subaddress << 5 | count & 0x1F


class Mil1553(unittest.TestCase):

    def bus(self, response: float = 4, data=(), bad=()):
        bench = Bench(pullups=1 << INH)                  # the transmitter off until driven
        bench.load(0, (HERE / "mil1553_bc.asm").read_text())
        rt = bench.add(Mil1553Rt(TX, TXB, INH, RX, RT, list(data), response=response, bad=bad))
        bench.start(1)
        return bench, rt

    def message(self, bench, send: list[int], receive: int) -> list[int]:
        start = len(bench.received[0])
        bench.send(0, [len(send) - 1, *send, receive - 1])
        bench.run(100_000, until=lambda: len(bench.received[0]) >= start + 2 * receive + 1)
        bench.run(500)
        return bench.received[0][start:]

    def test_receive_command(self):
        for response in (4, 12):
            with self.subTest(response=response):
                bench, rt = self.bus(response)
                data = [0x1234, 0xFFFF, 0x0000]
                got = self.message(bench, [command(RT, 0, 2, 3), *data], 1)
                self.assertEqual(rt.errors, [])
                self.assertEqual(rt.received, [data])
                self.assertEqual(got[0], RT << 11, "status word")
                self.assertTrue(got[1] & PARITY_OK)
                self.assertEqual(got[2:], [0xFFFF], "every word came")

    def test_transmit_command_with_a_parity_error(self):
        data = [0xBEEF, 0x8001, 0x7FFE, 0x0F0F]
        bench, rt = self.bus(12, data, bad=[2])
        got = self.message(bench, [command(RT, 1, 7, 4)], 5)
        self.assertEqual(rt.errors, [])
        self.assertEqual(got[0:-1:2], [RT << 11, *data])
        self.assertEqual([bool(st & PARITY_OK) for st in got[1:-1:2]], [True, True, True, False, True])
        self.assertEqual(got[-1], 0xFFFF)

    def test_no_response(self):
        bench, rt = self.bus()
        got = self.message(bench, [command(9, 1, 1, 2)], 3)
        self.assertEqual(rt.errors, [])
        self.assertEqual(got, [2], "three words missing (3 - 1), none received")
        self.assertEqual(rt.commands, [command(9, 1, 1, 2)])


if __name__ == "__main__":
    unittest.main()
