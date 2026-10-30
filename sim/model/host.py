"""host.py - The host side of HOST.md: loads, configures and runs the SMs only through SPI
transactions (Chip.spi), as the firmware of the board's microcontroller would.

    host = Host(chip)
    host.load(0, assemble(source), in_base=3)   # IMEM, every register, RESET, PC / X / Y
    host.start(0b01)
"""

from __future__ import annotations

from typing import Sequence

from asm import CONFIG_RESET, PAGE_WORDS, Program
from chip import (CLKDIV_FRAC, COND_CFG, CTRL, GPIO, OP_IMEM_WR, OP_REG_RD, OP_REG_WR, OP_RX_POP,
                  OP_TX_PUSH, PADS_BIDIR, PAU_CODE, PAU_MODE, PC, WRAP, Chip, header)


class Host:
    """SPI master of the chip (HOST.md)."""

    def __init__(self, chip: Chip) -> None:
        self.chip = chip

    def write(self, n: int, addr: int, *words: int) -> None:
        """REG_WR burst from `addr` of SM n (chip registers: any n)."""
        self.chip.spi(header(OP_REG_WR, n, addr), words)

    def read(self, n: int, addr: int, count: int = 1) -> list[int]:
        """REG_RD burst of `count` registers from `addr`."""
        return self.chip.spi(header(OP_REG_RD, n, addr), [0] * count)[1:]

    def load(self, n: int, program: Program, page: int = 0, **over) -> None:
        """Stop SM n, write its IMEM and every configuration register from the program (keyword
        arguments override its fields; jmp_pin moves the `pin` condition), RESET it and preset
        PC, X and Y. The SM is left stopped; the program's open-drain pins are added to GPIO.
        Open drain on a pin without a bidirectional pad is refused (PADS_BIDIR). A program of one
        page can go to any `page` (ISA.md, JMP): its wrap and start move with it."""
        if page and len(program) > PAGE_WORDS:
            raise ValueError("only a program of one page moves to another page")
        base = page * PAGE_WORDS
        unknown = set(over) - set(CONFIG_RESET) - {"jmp_pin"}
        if unknown:
            raise TypeError(f"unknown configuration fields: {sorted(unknown)}")
        c = {**program.config(), **over}
        cond = list(c["cond_cfg"])
        if "jmp_pin" in over and program.pin_slot is not None:
            cond[program.pin_slot] = cond[program.pin_slot] & 0xF0 | over["jmp_pin"] & 15
        if c["open_drain_mask"] & ~self.read(0, PADS_BIDIR)[0]:
            raise ValueError(f"open drain on pins without a bidirectional pad: {c['open_drain_mask']:#06x}")
        en = self.read(0, CTRL)[0] & ~(1 << n)
        self.write(0, CTRL, en)
        self.chip.spi(header(OP_IMEM_WR, n), [base, *program])
        self.write(n, WRAP,                                      # WRAP .. CLKDIV
                   (c["wrap_top"] + base) << 8 | c["wrap_bottom"] + base,
                   c["sideset_base"] << 8 | c["side_set_pindirs"] << 4 | c["side_set_opt"] << 3
                   | c["side_set_count"],
                   c["set_count"] << 12 | c["set_base"] << 8 | c["out_base"] << 4 | c["in_base"],
                   c["pin_mask"],
                   (c["pull_thresh"] & 0xF) << 12 | (c["push_thresh"] & 0xF) << 8
                   | c["shift_out_right"] << 1 | c["shift_in_right"],
                   c["clkdiv_int"])
        self.write(n, CLKDIV_FRAC, c["clkdiv_frac"])
        for slot, byte in enumerate(cond):
            self.write(n, COND_CFG, slot << 8 | byte)
        self.write(n, PAU_MODE, c["pau_mode"], c["crc_cfg"], c["crc_poly"] & 0xFFFF,   # .. CRC_RES_HI
                   c["crc_poly"] >> 16, c["crc_res"] & 0xFFFF, c["crc_res"] >> 16)
        self.write(n, PAU_CODE, c["pau_code"], c["sync"] & 0xFFFF, c["sync"] >> 16,   # .. PAU_CFG
                   c["pau_cfg"])
        if c["open_drain_mask"]:
            self.write(0, GPIO, self.read(0, GPIO)[0] | c["open_drain_mask"])
        self.write(0, CTRL, en | 1 << (4 + n))                   # RESET
        self.write(n, PC, c["start"] + base, c["x"], c["y"])     # PC, X, Y

    def start(self, mask: int) -> None:
        """Run the SMs of `mask` (bit i = SM i) from the same clock; stop the others."""
        self.write(0, CTRL, mask)

    def push(self, n: int, words: Sequence[int]) -> None:
        """TX_PUSH burst (words beyond a full FIFO are dropped)."""
        self.chip.spi(header(OP_TX_PUSH, n), words)

    def pop(self, n: int, count: int = 1) -> list[int]:
        """RX_POP burst (0 for each word the FIFO did not have)."""
        words: list[int] = []
        while len(words) < count:                    # RX_POP pops `Addr` words, up to 64
            k = min(count - len(words), 64)
            words += self.chip.spi(header(OP_RX_POP, n, k & 63), [0] * k)[1:]
        return words
