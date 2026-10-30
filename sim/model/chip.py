"""chip.py - Reference model of the whole openPIO chip: N state machines sharing the pins, and
the host interface of HOST.md (registers, SPI commands with bursts).

    chip = Chip(n_sm=2)
    chip.spi(header(OP_REG_WR, 0, CTRL), [0b01])    # run SM 0 (host.Host loads programs)
    chip.step()                                     # one clock
"""

from __future__ import annotations

from typing import Sequence

from asm import IMEM_WORDS
from emu import PIN_ALL, PinBus, StateMachine, pin_values

# Registers of an SM (HOST.md)
STATUS, WRAP, SIDESET, PINBASE, PIN_MASK, MISC, CLKDIV, COND_CFG = range(8)
COND_CFG01, COND_CFG23, PAU_MODE, CRC_CFG, CRC_POLY_LO, CRC_POLY_HI, CRC_RES_LO, CRC_RES_HI = range(8, 16)
(IRQ, PC, X, Y, ISR, OSR, COUNTS, COND01, COND23, CRC_LO, CRC_HI, SM_STATUS, FIFO_LEVEL, IRQ_MASK,
 CLKDIV_FRAC, TIME) = range(16, 32)
PAU_CODE, PAU_CFG = 0x30, 0x31
# Chip registers
CTRL, GPIO, PINS, PINS_OE, PINS_IN, IRQ_SUM, ID, PADS_OUT, PADS_IN, PADS_BIDIR = range(0x20, 0x2A)

# SPI commands
OP_NOP, OP_REG_WR, OP_REG_RD, OP_TX_PUSH, OP_RX_POP, OP_IMEM_WR, OP_IMEM_RD = 0, 2, 3, 4, 5, 6, 7

ISA_VERSION = 3
PAU_MODE_BITS, PAU_CFG_BITS, CRC_CFG_BITS = 0x0FFF, 0x007F, 0x7F     # the defined fields

# Pads of the 16 pin positions (HOST.md, Pins), Tiny Tapeout chip: pin k = uio[k] (k < 8), pin 8 + k
# = uo_out[k] out, ui_in[k] in; uo_out[1:0] and ui_in[2:0] belong to the host.
TT_PADS = (0xFCFF, 0xF8FF, 0x00FF)      # pins with an output pad, with an input pad, bidirectional


def sm_register(sm: StateMachine, addr: int, irq_mask: int) -> int:
    """Register `addr` (0x01..0x1F, 0x30..0x3F, HOST.md) of an SM; reserved ones and COND_CFG
    read 0."""
    c = sm.cond_cfg
    regs = {
        WRAP: lambda: sm.wrap_top << 8 | sm.wrap_bottom,
        SIDESET: lambda: (sm.sideset_base << 8 | sm.side_set_pindirs << 4 | sm.side_set_opt << 3
                          | sm.side_set_count),
        PINBASE: lambda: sm.set_count << 12 | sm.set_base << 8 | sm.out_base << 4 | sm.in_base,
        PIN_MASK: lambda: sm.pin_mask,
        MISC: lambda: (sm.pull_thresh << 12 | sm.push_thresh << 8 | sm.shift_out_right << 1
                       | sm.shift_in_right),
        CLKDIV: lambda: sm.clkdiv_cfg_int,
        CLKDIV_FRAC: lambda: sm.clkdiv_cfg_frac,
        COND_CFG01: lambda: c[1] << 8 | c[0],
        COND_CFG23: lambda: c[3] << 8 | c[2],
        PAU_MODE: lambda: sm.pau_mode,
        PAU_CODE: lambda: sm.pau_code,
        PAU_CFG: lambda: sm.pau_cfg,
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
        IRQ_MASK: lambda: irq_mask,
        TIME: lambda: sm.time,
    }
    return regs[addr]() & 0xFFFF if addr in regs else 0


def header(op: int, sm: int = 0, addr: int = 0) -> int:
    """16-bit SPI header (HOST.md)."""
    return (op & 7) << 13 | (sm & 3) << 11 | (addr & 0x3F)


class Chip:
    """openPIO chip: SMs, shared pins, host registers."""

    def __init__(self, n_sm: int = 2, fifo_depth: int = 4, spi_word_clocks: int = 96,
                 pads: tuple[int, int, int] = TT_PADS) -> None:
        """spi_word_clocks: clocks a 16-bit SPI word takes (SCLK = clk / 6: 96). pads: masks of
        the pins with an output pad, an input pad, and both on one bidirectional line."""
        assert n_sm in (2, 4), "an even number of SMs, at most 4 (CTRL has 4 bits per field)"
        self.bus = PinBus()
        self.sms = [StateMachine(bus=self.bus, index=i, fifo_depth=fifo_depth, buffer=True)
                    for i in range(n_sm)]
        for sm in self.sms:
            sm.peer = self.sms[sm.index ^ 1]
        self.irq_mask = [0xFF] * n_sm
        self.spi_word_clocks = spi_word_clocks
        self.pads_out, self.pads_in, self.pads_bidir = pads
        self.en = 0
        self.stepping = 0               # CTRL STEP: SM i runs until it has executed a strobe
        self.spi_sm = 0                 # the SM of the last SPI transaction (the header's MISO)
        self.pin_in = 0                 # levels at the input pads now, bit = pin
        self._sync = [0, 0]             # 2-flip-flop input synchronizer: gpio_in of 2 clocks ago
        self.cycle = 0

    # ------------------------------------------------------------------ clock and pins

    @property
    def gpio_in(self) -> int:
        """What the SMs see: the input pins two clocks ago (synchronizer)."""
        return self._sync[0]

    def step(self, cycles: int = 1) -> None:
        """Advance `cycles` clocks (ISA.md, Timing within a clock)."""
        for _ in range(cycles):
            gpio_in = self._sync[0]
            self._sync = [self._sync[1], self.pin_in & self.pads_in]
            for sm in self.sms:
                sm.gpio_in = gpio_in
                sm.peer_irq_flags = list(sm.peer.irq_flags)     # as at the clock start
            for sm in self.sms:
                if (self.en | self.stepping) >> sm.index & 1:
                    sm.step()
                    if sm.strobed:
                        self.stepping &= ~(1 << sm.index)
            for sm in self.sms:
                sm.end_clock()
            self.bus.commit()
            for sm in self.sms:
                sm.buffer_clock()
            self.cycle += 1

    @property
    def pins(self) -> tuple[int, int]:
        """(values, output enables) at the output pads, bit = pin. A bidirectional pad has the
        direction latch and open drain; an output-only pad always drives the pin value."""
        out, od = pin_values(self.bus, self.sms), self.bus.od_mask
        only = self.pads_out & ~self.pads_bidir
        bidir_out, bidir_oe = out & ~od, self.bus.dirs & ~(out & od)
        return ((bidir_out & self.pads_bidir | out & only) & PIN_ALL,
                (bidir_oe & self.pads_bidir | only) & PIN_ALL)

    @property
    def host_irq(self) -> int:
        return int(any(self._irq_pending(sm) for sm in self.sms))

    def _irq_pending(self, sm: StateMachine) -> bool:
        """A flag in IRQ_MASK [7:0], RX FIFO not empty with [8], TX FIFO <= 64 words with [9]."""
        m = self.irq_mask[sm.index]
        return (any(f and m >> i & 1 for i, f in enumerate(sm.irq_flags))
                or bool(m >> 8 & 1 and sm.rx_level) or bool(m >> 9 & 1 and sm.tx_level <= 64))

    @property
    def uo_out(self) -> int:
        """Tiny Tapeout uo_out without MISO: pins 10..15 on [7:2], HOST_IRQ on [1]."""
        return (self.pins[0] >> 8 & 0xFC) | self.host_irq << 1

    # ------------------------------------------------------------------ host registers

    def _sm_status_reg(self, sm: StateMachine) -> int:
        return (int(sm.tx_level == 0) | int(not sm.tx_room()) << 1
                | int(sm.rx_level == 0) << 2 | sm._sm_status(7) << 3
                | int(sm.stalled) << 4 | (self.en >> sm.index & 1) << 5 | sm.pc << 8)

    def reg_read(self, n: int, addr: int) -> int:
        if 0x20 <= addr < 0x30:
            return self._chip_read(addr)
        sm = self.sms[n]
        if addr == STATUS:
            return self._sm_status_reg(sm)
        return sm_register(sm, addr, self.irq_mask[n])

    def reg_write(self, n: int, addr: int, val: int) -> None:
        val &= 0xFFFF
        if 0x20 <= addr < 0x30:
            return self._chip_write(addr, val)
        sm = self.sms[n]
        stopped = not self.en >> n & 1
        if addr == WRAP:
            sm.wrap_bottom, sm.wrap_top = val & 0x3F, val >> 8 & 0x3F
        elif addr == SIDESET:
            sm.side_set_count, sm.side_set_opt = val & 7, bool(val >> 3 & 1)
            sm.side_set_pindirs, sm.sideset_base = bool(val >> 4 & 1), val >> 8 & 15
        elif addr == PINBASE:
            sm.in_base, sm.out_base = val & 15, val >> 4 & 15
            sm.set_base, sm.set_count = val >> 8 & 15, val >> 12 & 7
        elif addr == PIN_MASK:
            sm.pin_mask = val
        elif addr == MISC:
            sm.shift_in_right, sm.shift_out_right = bool(val & 1), bool(val >> 1 & 1)
            sm.push_thresh, sm.pull_thresh = val >> 8 & 0xF, val >> 12 & 0xF
        elif addr == CLKDIV:
            sm.clkdiv_cfg_int = val or 1
        elif addr == CLKDIV_FRAC:
            sm.clkdiv_cfg_frac = val & 0xFF
        elif addr == COND_CFG:
            sm.cond_cfg[val >> 8 & 3] = val & 0xFF
        elif addr == PAU_MODE:
            sm.pau_mode = val & PAU_MODE_BITS
        elif addr == PAU_CODE:
            sm.pau_code = val & 0xFFFF
        elif addr == PAU_CFG:
            sm.pau_cfg = val & PAU_CFG_BITS
        elif addr == CRC_CFG:
            sm.crc_cfg = val & CRC_CFG_BITS
        elif addr == CRC_POLY_LO:
            sm.crc_poly = sm.crc_poly & 0xFFFF0000 | val
        elif addr == CRC_POLY_HI:
            sm.crc_poly = sm.crc_poly & 0xFFFF | val << 16
        elif addr == CRC_RES_LO:
            sm.crc_res = sm.crc_res & 0xFFFF0000 | val
        elif addr == CRC_RES_HI:
            sm.crc_res = sm.crc_res & 0xFFFF | val << 16
        elif addr == IRQ:                           # clear, then set
            for i in range(8):
                if val >> i & 1:
                    sm.irq_flags[i] = False
                if val >> (8 + i) & 1:
                    sm.irq_flags[i] = True
        elif addr == IRQ_MASK:
            self.irq_mask[n] = val & 0x3FF
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
            return self.bus.od_mask
        if addr in (PINS, PINS_OE):
            return self.pins[addr - PINS]
        if addr == PINS_IN:
            return self.gpio_in
        if addr == IRQ_SUM:
            return sum(int(self._irq_pending(sm)) << sm.index for sm in self.sms)
        if addr == ID:
            return ISA_VERSION << 12 | len(self.sms) << 8 | 0x50
        if addr in (PADS_OUT, PADS_IN, PADS_BIDIR):
            return (self.pads_out, self.pads_in, self.pads_bidir)[addr - PADS_OUT]
        return 0

    def _chip_write(self, addr: int, val: int) -> None:
        if addr == CTRL:
            mask = (1 << len(self.sms)) - 1
            self.en = val & mask
            for sm in self.sms:
                if val >> (4 + sm.index) & 1:
                    sm.reset()
            self.stepping |= val >> 8 & mask & ~self.en
        elif addr == GPIO:
            self.bus.od_mask = val

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
        """0 while SM n runs: its IMEM port fetches instructions."""
        if self.en >> n & 1:
            return 0
        return self.sms[n].memory[addr % IMEM_WORDS]

    # ------------------------------------------------------------------ SPI transaction

    def host(self, op: int, n: int, addr: int = 0, word: int = 0) -> int:
        """One host access, in one clock (ISA.md, Timing within a clock): its reads take the
        state at the clock start (an RX_POP before the SMs and the FIFO moves), its writes land
        at the end, after the SMs' and the FIFO moves. Returns the word read (0 for a write)."""
        sm = self.sms[n]
        value, where = 0, None
        if op == OP_REG_RD:
            value = self.reg_read(n, addr)
        elif op == OP_IMEM_RD:
            value = self.imem_read(n, addr)
        elif op == OP_RX_POP:
            value = self.rx_pop(n)
        elif op == OP_TX_PUSH:
            where = sm.tx_where()
        self.step()
        if op == OP_REG_WR:
            self.reg_write(n, addr, word)
        elif op == OP_IMEM_WR:
            self.imem_write(n, addr, word)
        elif where is not None:
            sm.tx_place(word, where)
        return value

    def spi(self, hdr: int, words: Sequence[int] = ()) -> list[int]:
        """One SPI transaction (HOST.md): header then a burst of data words, each taking
        spi_word_clocks clocks. A read word is a host access in its first clock, a write one in
        the clock after its last bit; RX_POP pops `Addr` words (0 = 64), later ones read 0;
        IMEM_WR / IMEM_RD take their address in the first data word.
        Returns the MISO words: the header's (the previous transaction's SM), then one per data
        word."""
        op, n, addr = hdr >> 13 & 7, hdr >> 11 & 3, hdr & 0x3F
        n = min(n, len(self.sms) - 1)
        miso = [self._sm_status_reg(self.sms[self.spi_sm])]
        self.spi_sm = n
        pops = (addr or 64) if op == OP_RX_POP else 0
        self.step(self.spi_word_clocks)
        if op in (OP_IMEM_WR, OP_IMEM_RD) and words:        # the first data word: the address
            addr = words[0] % IMEM_WORDS
            miso.append(self._sm_status_reg(self.sms[n]))
            self.step(self.spi_word_clocks)
            words = words[1:]
        for word in words:
            if op == OP_RX_POP and not pops:
                miso.append(0)
                self.step(self.spi_word_clocks)
                continue
            pops -= op == OP_RX_POP
            if op in (OP_REG_RD, OP_RX_POP, OP_IMEM_RD):
                miso.append(self.host(op, n, addr))
                addr += op != OP_RX_POP
                self.step(self.spi_word_clocks - 1)
                continue
            miso.append(self._sm_status_reg(self.sms[n]))
            self.step(self.spi_word_clocks - 1)
            if op in (OP_REG_WR, OP_TX_PUSH, OP_IMEM_WR):
                self.host(op, n, addr, word)
                addr += op != OP_TX_PUSH
            else:
                self.step()
        return miso
