#!/usr/bin/env python3
"""ISO/IEC 7816-3 smart-card reader against a reference card (T = 0, direct convention,
F = 372, D = 1): the card's 5 MHz clock from the other SM, cold reset (RST low 400 card clocks
or more), the answer to reset, then a T = 0 command header and the card's answer (procedure
byte, data, 90 00), every bit sampled by the card at 0.2, 0.5 and 0.8 ETU, guard times of 12
ETU between characters and 16 between directions. Error signalling both ways: a card character
with the wrong parity, signalled by the reader at 10.5 ETU and repeated by the card; a reader
character the card signals, repeated by the reader. A protocol written after the ISA was frozen
(2026-10-28), without changing the hardware."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
sys.path.insert(0, str(HERE.parents[1] / "sim" / "model"))
from bench import Bench                                  # noqa: E402
from devices import IsoCard                              # noqa: E402

IO, CLK, RST = 0, 1, 2                                   # pins as in the programs
ATR = [0x3B, 0x02, 0x14, 0x50]                           # T = 0, two historical bytes
HEADER = [0x00, 0xB0, 0x00, 0x00, 0x04]                  # READ BINARY, 4 bytes
DATA = [0xDE, 0xAD, 0xBE, 0xEF]
ERROR = 1 << 15                                          # a character with a parity error


class Iso7816(unittest.TestCase):

    def test_reset_command_and_error_signalling(self):
        bench = Bench(pullups=1 << IO)
        bench.load(0, (HERE / "iso7816_reader.asm").read_text())
        bench.load(1, (HERE / "iso7816_clk.asm").read_text())
        card = bench.add(IsoCard(IO, CLK, RST, ATR, DATA, bad_parity=[2], reject=[3]))
        bench.start(0b11)                                # the clock and the reader together
        bench.run(2_000_000, until=lambda: len(bench.received[0]) >= len(ATR) + 1)
        atr = bench.received[0][:]
        self.assertEqual([w & 0xFF for w in atr if not w & ERROR], ATR)
        self.assertEqual([w & 0xFF for w in atr if w & ERROR], [ATR[2]],
                         "the bad character, flagged, then again")
        bench.run(20 * 3720)                             # the card's turn ends: 16 ETU
        bench.send(0, HEADER)
        answer = [HEADER[1], *DATA, 0x90, 0x00]
        bench.run(3_000_000, until=lambda: len(bench.received[0]) >= len(atr) + len(answer))
        bench.run(20 * 3720)
        self.assertEqual(card.errors, [])
        self.assertEqual(card.received, HEADER)
        self.assertEqual([w & 0x1FF for w in bench.received[0][len(atr):]],
                         [b | (bin(b).count("1") & 1) << 8 for b in answer], "data and parity")
        self.assertEqual((card.reader_signals, card.card_signals), (1, 1))
        self.assertEqual(bench.errors(), [])


if __name__ == "__main__":
    unittest.main()
