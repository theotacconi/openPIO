"""chip.py - Reference model of the whole openPIO chip: N state machines sharing the pins, and
the host interface of HOST.md (registers, SPI commands with bursts).

    chip = Chip(n_sm=2)
    chip.load(0, asm.assemble(source))      # IMEM + configuration of SM 0
    chip.reg_write(0, CTRL, 0b01)           # run SM 0
    chip.step()                             # one clock
"""

from __future__ import annotations

from typing import Sequence

from asm import IMEM_WORDS
from emu import PinBus, StateMachine, pad_outputs

# Registers of an SM (HOST.md)
STATUS, WRAP, SIDESET, PINBASE, PIN_MASK, MISC, CLKDIV, COND_CFG = range(8)
COND_CFG01, COND_CFG23, PAU_MODE, CRC_CFG, CRC_POLY_LO, CRC_POLY_HI, CRC_RES_LO, CRC_RES_HI = range(8, 16)
IRQ, PC, X, Y, ISR, OSR, COUNTS, COND01, COND23, CRC_LO, CRC_HI, SM_STATUS, FIFO_LEVEL = range(16, 29)
# Chip registers
CTRL, GPIO, PINS, PINS_IN, IRQ_SUM, ID = range(0x20, 0x26)

# SPI commands
OP_NOP, OP_REG_WR, OP_REG_RD, OP_TX_PUSH, OP_RX_POP, OP_IMEM_WR, OP_IMEM_RD = 0, 2, 3, 4, 5, 6, 7

ISA_VERSION = 2


def header(op: int, sm: int = 0, addr: int = 0) -> int:
    """16-bit SPI header (HOST.md)."""
    return (op & 7) << 13 | (sm & 3) << 11 | (addr & 0x3F)


class Chip:
    """openPIO chip: SMs, shared pins, host registers."""

    def __init__(self, n_sm: int = 2, fifo_depth: int = 4) -> None:
        assert n_sm in (2, 4), "an even number of SMs, at most 4 (CTRL has 4 bits per field)"
        self.bus = PinBus()
        self.sms = [StateMachine(bus=self.bus, index=i, fifo_depth=fifo_depth, buffer=True)
                    for i in range(n_sm)]
        for sm in self.sms:
            sm.peer = self.sms[sm.index ^ 1]
            sm.peer_irq_flags = sm.peer.irq_flags
        self.en = 0
        self.in_sel = 0
        self.uio_in = 0
        self.ui_in = 0
        self.cycle = 0

    # ------------------------------------------------------------------ clock and pins

    @property
    def gpio_in(self) -> int:
        sel = (self.in_sel << 3) & 0xF8
        return (self.uio_in & ~sel | self.ui_in & sel) & 0xFF

    def step(self, cycles: int = 1) -> None:
        """Advance `cycles` clocks: the enabled SMs in parallel, pin writes highest SM last."""
        for _ in range(cycles):
            gpio_in = self.gpio_in
            for sm in self.sms:
                sm.gpio_in = gpio_in
            for sm in self.sms:
                if self.en >> sm.index & 1:
                    sm.step()
            self.bus.commit()
            for sm in self.sms:
                sm.buffer_clock()
            self.cycle += 1

    @property
    def pins(self) -> tuple[int, int]:
        """(uio_out, uio_oe)."""
        return pad_outputs(self.bus, self.sms)

    @property
    def host_irq(self) -> int:
        return int(any(any(sm.irq_flags) for sm in self.sms))

    @property
    def uo_out(self) -> int:
        return (self.pins[0] & 0xFC) | self.host_irq << 1

    # ------------------------------------------------------------------ loading

    def load(self, n: int, program: Sequence[int], **cfg) -> StateMachine:
        """Write a program and the configuration it carries (asm.Program) into SM n, as a host
        would over SPI, then RESET it. Extra keyword arguments as StateMachine attributes."""
        sm = self.sms[n]
        for addr, word in enumerate(program):
            self.imem_write(n, addr, word)
        sm.load_program(program)
        if getattr(program, "pin_slot", None) is not None and "jmp_pin" in cfg:
            s = program.pin_slot
            sm.cond_cfg[s] = (sm.cond_cfg[s] & 0xF0) | (cfg.pop("jmp_pin") & 7)
        for key, val in cfg.items():
            setattr(sm, key, val)
        sm.reset()
        return sm

    # ------------------------------------------------------------------ host registers

    def _sm_status_reg(self, sm: StateMachine) -> int:
        return (int(sm.tx_level == 0) | int(not sm.tx_room()) << 1
                | int(sm.rx_level == 0) << 2 | sm._sm_status(7) << 3
                | int(sm.stalled) << 4 | (self.en >> sm.index & 1) << 5 | sm.pc << 8)

    def reg_read(self, n: int, addr: int) -> int:
        if addr >= 0x20:
            return self._chip_read(addr)
        sm = self.sms[n]
        c = sm.cond_cfg
        regs = {
            STATUS: lambda: self._sm_status_reg(sm),
            WRAP: lambda: sm.wrap_top << 8 | sm.wrap_bottom,
            SIDESET: lambda: (sm.sideset_base << 8 | sm.side_set_pindirs << 4 | sm.side_set_opt << 3
                              | sm.side_set_count),
            PINBASE: lambda: sm.set_count << 12 | sm.set_base << 8 | sm.out_base << 4 | sm.in_base,
            PIN_MASK: lambda: sm.pin_mask,
            MISC: lambda: (sm.pull_thresh << 12 | sm.push_thresh << 8 | sm.shift_out_right << 1
                           | sm.shift_in_right),
            CLKDIV: lambda: sm.clkdiv_cfg_frac << 8 | sm.clkdiv_cfg_int,
            COND_CFG01: lambda: c[1] << 8 | c[0],
            COND_CFG23: lambda: c[3] << 8 | c[2],
            PAU_MODE: lambda: sm.pau_mode,
            CRC_CFG: lambda: sm.crc_cfg,
            CRC_POLY_LO: lambda: sm.crc_poly & 0xFFFF,
            CRC_POLY_HI: lambda: sm.crc_poly >> 16,
            CRC_RES_LO: lambda: sm.crc_res & 0xFFFF,
            CRC_RES_HI: lambda: sm.crc_res >> 16,
            IRQ: lambda: sum(int(f) << i for i, f in enumerate(sm.irq_flags)),
            PC: lambda: sm.pc,
            X: lambda: sm.x,
            Y: lambda: sm.y,
            ISR: lambda: sm.isr,
            OSR: lambda: sm.osr,
            COUNTS: lambda: sm.osr_count << 8 | sm.isr_count,
            COND01: lambda: sm.cond[1] << 8 | sm.cond[0],
            COND23: lambda: sm.cond[3] << 8 | sm.cond[2],
            CRC_LO: lambda: sm.crc & 0xFFFF,
            CRC_HI: lambda: sm.crc >> 16,
            SM_STATUS: lambda: sm.status_word(),
            FIFO_LEVEL: lambda: sm.rx_level << 8 | sm.tx_level,
        }
        return regs[addr]() & 0xFFFF if addr in regs else 0

    def reg_write(self, n: int, addr: int, val: int) -> None:
        val &= 0xFFFF
        if addr >= 0x20:
            return self._chip_write(addr, val)
        sm = self.sms[n]
        stopped = not self.en >> n & 1
        if addr == WRAP:
            sm.wrap_bottom, sm.wrap_top = val & 0x3F, val >> 8 & 0x3F
        elif addr == SIDESET:
            sm.side_set_count, sm.side_set_opt = val & 7, bool(val >> 3 & 1)
            sm.side_set_pindirs, sm.sideset_base = bool(val >> 4 & 1), val >> 8 & 7
        elif addr == PINBASE:
            sm.in_base, sm.out_base = val & 7, val >> 4 & 7
            sm.set_base, sm.set_count = val >> 8 & 7, val >> 12 & 7
        elif addr == PIN_MASK:
            sm.pin_mask = val & 0xFF
        elif addr == MISC:
            sm.shift_in_right, sm.shift_out_right = bool(val & 1), bool(val >> 1 & 1)
            sm.push_thresh, sm.pull_thresh = val >> 8 & 0xF, val >> 12 & 0xF
        elif addr == CLKDIV:
            sm.clkdiv_cfg_int, sm.clkdiv_cfg_frac = (val & 0x1F) or 1, val >> 8
        elif addr == COND_CFG:
            sm.cond_cfg[val >> 8 & 3] = val & 0xFF
        elif addr == PAU_MODE:
            sm.pau_mode = val
        elif addr == CRC_CFG:
            sm.crc_cfg = val
        elif addr == CRC_POLY_LO:
            sm.crc_poly = sm.crc_poly & 0xFFFF0000 | val
        elif addr == CRC_POLY_HI:
            sm.crc_poly = sm.crc_poly & 0xFFFF | val << 16
        elif addr == CRC_RES_LO:
            sm.crc_res = sm.crc_res & 0xFFFF0000 | val
        elif addr == CRC_RES_HI:
            sm.crc_res = sm.crc_res & 0xFFFF | val << 16
        elif addr == IRQ:
            for i in range(8):
                if val >> i & 1:
                    sm.irq_flags[i] = False
        elif stopped and addr == PC:
            sm.pc = val % IMEM_WORDS
        elif stopped and addr == X:
            sm.x = val
        elif stopped and addr == Y:
            sm.y = val
        elif stopped and addr == ISR:
            sm.isr = val
        elif stopped and addr == OSR:
            sm.osr = val
        elif stopped and addr == COUNTS:
            sm.isr_count, sm.osr_count = min(val & 0x1F, 16), min(val >> 8 & 0x1F, 16)

    def _chip_read(self, addr: int) -> int:
        if addr == CTRL:
            return self.en
        if addr == GPIO:
            return self.in_sel << 11 | self.bus.od_mask
        if addr == PINS:
            out, oe = self.pins
            return oe << 8 | out
        if addr == PINS_IN:
            return self.gpio_in
        if addr == IRQ_SUM:
            return sum(int(any(sm.irq_flags)) << sm.index for sm in self.sms)
        if addr == ID:
            return ISA_VERSION << 12 | len(self.sms) << 8 | 0x50
        return 0

    def _chip_write(self, addr: int, val: int) -> None:
        if addr == CTRL:
            mask = (1 << len(self.sms)) - 1
            self.en = val & mask
            for sm in self.sms:
                if val >> (4 + sm.index) & 1:
                    sm.reset()
            for sm in self.sms:
                if val >> (8 + sm.index) & 1 and not self.en >> sm.index & 1:
                    self._step_one(sm)
        elif addr == GPIO:
            self.bus.od_mask, self.in_sel = val & 0xFF, val >> 11 & 0x1F

    def _step_one(self, sm: StateMachine) -> None:
        """STEP: clock SM until it has executed one strobe."""
        sm.gpio_in = self.gpio_in
        while True:
            sm.step()
            self.bus.commit()
            if sm.strobed:
                return

    # ------------------------------------------------------------------ FIFOs and IMEM

    def tx_push(self, n: int, word: int) -> bool:
        return self.sms[n].tx_write(word)

    def rx_pop(self, n: int) -> int:
        word = self.sms[n].rx_read()
        return 0 if word is None else word

    def imem_write(self, n: int, addr: int, word: int) -> None:
        if not self.en >> n & 1:
            self.sms[n].memory[addr % IMEM_WORDS] = word & 0xFFFF

    def imem_read(self, n: int, addr: int) -> int:
        return self.sms[n].memory[addr % IMEM_WORDS]

    # ------------------------------------------------------------------ SPI transaction

    def spi(self, hdr: int, words: Sequence[int] = ()) -> list[int]:
        """One SPI transaction (HOST.md): header then a burst of data words. Returns the MISO
        words: the header's, then one per data word."""
        op, n, addr = hdr >> 13 & 7, hdr >> 11 & 3, hdr & 0x3F
        n = min(n, len(self.sms) - 1)
        miso = [self._sm_status_reg(self.sms[n])]
        for word in words:
            if op == OP_REG_RD:
                miso.append(self.reg_read(n, addr))
                addr += 1
            elif op == OP_RX_POP:
                miso.append(self.rx_pop(n))
            elif op == OP_IMEM_RD:
                miso.append(self.imem_read(n, addr))
                addr += 1
            else:
                miso.append(self._sm_status_reg(self.sms[n]))
                if op == OP_REG_WR:
                    self.reg_write(n, addr, word)
                    addr += 1
                elif op == OP_TX_PUSH:
                    self.tx_push(n, word)
                elif op == OP_IMEM_WR:
                    self.imem_write(n, addr, word)
                    addr += 1
        return miso
