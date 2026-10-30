#!/usr/bin/env python3
"""SD card commands in SPI mode, judged by the reference SPI slave in each of the four modes:
the PAU's bit clock on SCK (idle at CPOL, MOSI set up before every sampling edge, held after
it), CS_N around each command, and the CRC-7 the PAU appends checked against CRC-7/SD (CMD0
ends in 0x95, CMD8 with 0x1AA in 0x87). A protocol written after the ISA v3 revision."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
sys.path.insert(0, str(HERE.parents[1] / "sim" / "model"))
from asm import assemble                                 # noqa: E402
from bench import Bench                                  # noqa: E402
from devices import SpiSlave, crc7_sd                    # noqa: E402

MOSI, SCK, CS, MISO = 0, 1, 2, 3
COMMANDS = [(0, 0), (8, 0x1AA), (55, 0), (41, 0x40000000), (17, 0x00012345)]


def command_bytes(index: int, arg: int) -> list[int]:
    head = [0x40 | index, *(arg >> s & 0xFF for s in (24, 16, 8, 0))]
    return head + [crc7_sd(head) << 1 | 1]


class SdCommands(unittest.TestCase):

    def test_commands_in_every_spi_mode(self):
        prog = assemble((HERE / "sd_command.asm").read_text())
        for mode in range(4):
            with self.subTest(mode=mode):
                bench = Bench(pullups=1 << CS)            # CS_N pulled up on the board
                bench.load(0, prog, pau_mode=prog.cfg["pau_mode"] | (mode >> 1) << 5 | (mode & 1) << 6)
                slave = bench.add(SpiSlave(SCK, MOSI, MISO, mode=mode, bits=8, cs=CS, setup=2))
                bench.start(1)
                for index, arg in COMMANDS:
                    bench.send(0, [b << 8 for b in command_bytes(index, arg)[:5]])
                    bench.run(8 * 6 * 8 + 40)
                self.assertEqual(slave.words, [b for c in COMMANDS for b in command_bytes(*c)])
                self.assertEqual(slave.words[5], 0x95, "CMD0")
                self.assertEqual(bench.errors(), [])


if __name__ == "__main__":
    unittest.main()
