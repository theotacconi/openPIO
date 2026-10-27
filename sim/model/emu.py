"""emu.py - Cycle-accurate emulator of one openPIO state machine, ISA v2 (ISA.md).

step() is one clock. On a strobe (every Nth clock, CLKDIV), in order: the PAU advances (TX line, RX
sampling), then the SM delays, re-evaluates a stall, or executes the instruction at PC; at the
end the condition values are kept for WAIT edges. Traces are concise fixed-width rows.
"""

from __future__ import annotations

import argparse
from collections import deque
import struct
from typing import Sequence

import asm
from asm import (
    CS_GPIO, CS_INBASE, CS_IRQ, CS_PAU, CS_PEER, CS_STATUS, CS_XBIT, CS_YBIT,
    COND_RESET, CT_FIFO, CT_IRQ, CT_PAU, CT_WAIT, IMEM_WORDS,
    JC_ALWAYS, JC_NEVER, JC_XDEC, JC_YDEC,
    OP_ALU, OP_CTRL, OP_IN, OP_JMP, OP_OUT, OP_SET,
    R_ISR, R_NULL, R_OSR, R_PINDIRS, R_PINS, R_SPECIAL, R_X, R_Y,
    disassemble,
)

HEADER = "CYC  | PC | INSTR              | DLY | X      | Y      | OSR    | ISR    | PIN_IN | PIN_OUT | STALL"
SEPARATOR = "-----+----+--------------------+-----+--------+--------+--------+--------+--------+---------+------"

PAU_QUEUE_BITS = 32
BUFFER_WORDS = 128                  # SRAM part of each FIFO (ISA.md, FIFOs)


def _rev16(v: int) -> int:
    return int(f"{v & 0xFFFF:016b}"[::-1], 2)


class _Stuffer:
    """Run counter for bit stuffing (ISA.md, PAU): after `length` qualifying bits, a stuffed bit."""

    def __init__(self) -> None:
        self.run = 0
        self.last: int | None = None

    def due(self, mode: int, length: int) -> int | None:
        """Value of the stuffed bit owed now, or None."""
        if self.run < length:
            return None
        return {0: 0, 1: 1}.get(mode, 1 - (self.last or 0))

    def count(self, bit: int, mode: int) -> None:
        """Account for a bit sent / received (data or stuffed)."""
        if mode == 2:
            self.run = self.run + 1 if bit == self.last else 1
        else:
            self.run = self.run + 1 if bit == (1 if mode == 0 else 0) else 0
        self.last = bit

    def stuffed(self, bit: int, mode: int) -> None:
        """A stuffed bit went by: in modes 00 / 01 it ends the run, in 10 it starts one."""
        if mode == 2:
            self.run, self.last = 1, bit
        else:
            self.run, self.last = 0, bit


class PinBus:
    """Pin output and direction latches, shared by the SMs of a chip (ISA.md, Multiple state
    machines). Writes made during a clock are applied at its end in SM order: the highest SM wins.
    An SM reads the latches of the clock start with its own writes applied."""

    def __init__(self) -> None:
        self.out = 0
        self.dirs = 0
        self.od_mask = 0
        self._writes: list[tuple[int, int, int, int]] = []   # (sm, latch: 0 out / 1 dirs, mask, value)

    def view(self, sm: int, latch: int) -> int:
        v = self.dirs if latch else self.out
        for i, lt, m, val in self._writes:
            if i == sm and lt == latch:
                v = (v & ~m) | (val & m)
        return v & 0xFF

    def write(self, sm: int, latch: int, mask: int, value: int) -> None:
        self._writes.append((sm, latch, mask & 0xFF, value & 0xFF))

    def commit(self) -> None:
        for _, lt, m, val in sorted(self._writes, key=lambda w: w[0]):
            if lt:
                self.dirs = (self.dirs & ~m) | (val & m)
            else:
                self.out = (self.out & ~m) | (val & m)
        self._writes.clear()


def pad_outputs(bus: PinBus, sms: Sequence["StateMachine"]) -> tuple[int, int]:
    """(values, output enables) of the 8 pins: latches, PAU TX overrides in SM order, open drain."""
    out, oe = bus.out, bus.dirs
    for sm in sms:
        out = sm.pau_override(out)
    od = bus.od_mask
    return out & ~od & 0xFF, oe & ~(out & od) & 0xFF        # open drain: enabled and 0 pulls low


class StateMachine:
    """Cycle-accurate emulator of one state machine. Standalone it owns its pins; in a
    chip.Chip it shares a PinBus and has a peer (ISA.md, Multiple state machines)."""

    def __init__(
        self,
        program: Sequence[int] | None = None,
        fifo_depth: int = 4,
        wrap_bottom: int = 0,
        wrap_top: int | None = None,
        side_set_count: int = 0,
        side_set_opt: bool = False,
        side_set_pindirs: bool = False,
        open_drain_mask: int = 0,
        clkdiv_int: int = 1,
        clkdiv_frac: int = 0,
        in_base: int = 0,
        out_base: int = 0,
        set_base: int = 0,
        set_count: int = 5,
        sideset_base: int = 0,
        jmp_pin: int | None = None,
        cond_cfg: Sequence[int] | None = None,
        push_thresh: int = 0,
        pull_thresh: int = 0,
        pau_mode: int = 0,
        crc_cfg: int = 0,
        crc_poly: int = 0,
        crc_res: int = 0,
        pin_mask: int = 0xFF,
        bus: PinBus | None = None,
        index: int = 0,
        buffer: bool = False,
        instructions: Sequence[int] | None = None,
    ) -> None:
        if program is None and instructions is not None:
            program = instructions
        self.memory: list[int] = [0] * IMEM_WORDS
        self.index = index
        self._own_bus = bus is None
        self.bus = bus if bus is not None else PinBus()
        self.pin_mask = pin_mask & 0xFF
        self.strobed = False                            # the last step() was a strobe

        # Host configuration
        self.wrap_bottom = wrap_bottom
        self.wrap_top = wrap_top if wrap_top is not None else IMEM_WORDS - 1
        self.side_set_count = side_set_count
        self.side_set_opt = side_set_opt
        self.side_set_pindirs = side_set_pindirs
        if open_drain_mask:
            self.bus.od_mask = open_drain_mask & 0xFF
        self.clkdiv_cfg_int = clkdiv_int if clkdiv_int > 0 else 1
        self.clkdiv_cfg_frac = clkdiv_frac & 0xFF
        self.in_base, self.out_base, self.set_base = in_base, out_base, set_base
        self.set_count, self.sideset_base = set_count, sideset_base
        self.shift_in_right = True
        self.shift_out_right = True
        self.push_thresh, self.pull_thresh = push_thresh & 0xF, pull_thresh & 0xF
        self.cond_cfg: list[int] = list(cond_cfg if cond_cfg is not None else COND_RESET)
        self.pau_mode, self.crc_cfg = pau_mode & 0xFFFF, crc_cfg & 0xFFFF
        self.crc_poly, self.crc_res = crc_poly & 0xFFFFFFFF, crc_res & 0xFFFFFFFF

        self.fifo_depth = fifo_depth
        self.tx_fifo: deque[int] = deque(maxlen=fifo_depth)     # fronts, next to the SM
        self.rx_fifo: deque[int] = deque(maxlen=fifo_depth)
        self.buffer = buffer                                      # SRAM parts (chip SMs)
        self.tx_back: deque[int] = deque()
        self.rx_back: deque[int] = deque()
        self.tx_flight: int | None = None                         # TX word moving to the front
        self.peer: StateMachine | None = None
        self.gpio_in = 0
        self.irq_flags = [False] * 8
        # The peer's IRQ flags and FIFOs; a chip links them to SM index ^ 1
        self.peer_irq_flags = [False] * 8
        self.peer_tx_fifo: deque[int] = deque(maxlen=fifo_depth)
        self.peer_rx_fifo: deque[int] = deque(maxlen=fifo_depth)
        self.history: deque[str] = deque(maxlen=15)

        if program is not None:
            self.load_program(program)
        if jmp_pin is not None and getattr(program, "pin_slot", None) is not None:
            s = program.pin_slot
            self.cond_cfg[s] = (self.cond_cfg[s] & 0xF0) | (jmp_pin & 7)
        self.reset()

    # ------------------------------------------------------------------ configuration

    @property
    def X(self) -> int:
        return self.x

    @X.setter
    def X(self, val: int) -> None:
        self.x = val & 0xFFFF

    @property
    def Y(self) -> int:
        return self.y

    @Y.setter
    def Y(self, val: int) -> None:
        self.y = val & 0xFFFF

    @property
    def PC(self) -> int:
        return self.pc

    @PC.setter
    def PC(self, val: int) -> None:
        self.pc = val % IMEM_WORDS

    @property
    def gpio_out(self) -> int:
        """Pin output latch as this SM sees it."""
        return self.bus.view(self.index, 0)

    @gpio_out.setter
    def gpio_out(self, val: int) -> None:
        self.bus.out = val & 0xFF

    @property
    def pindirs(self) -> int:
        """Pin direction latch as this SM sees it."""
        return self.bus.view(self.index, 1)

    @pindirs.setter
    def pindirs(self, val: int) -> None:
        self.bus.dirs = val & 0xFF

    @property
    def open_drain_mask(self) -> int:
        return self.bus.od_mask

    @open_drain_mask.setter
    def open_drain_mask(self, val: int) -> None:
        self.bus.od_mask = val & 0xFF

    @property
    def clkdiv_frac(self) -> int:
        return self._clkdiv_frac

    @clkdiv_frac.setter
    def clkdiv_frac(self, val: int) -> None:
        self._clkdiv_frac = val & 0xFF

    @property
    def tx_drives(self) -> bool:
        """The PAU drives the TX pins: TX on, or off and still draining its queue."""
        if self.tx_bit_err:
            return False
        return (bool(self.pau_ctl & 1) or self.tx_active or bool(self.tx_queue)
                or self.tx_stuff_pending is not None)

    def pau_override(self, out: int) -> int:
        """Pin values with this SM's PAU TX line on out_base (+1) while it drives, in PIN_MASK."""
        if not self.tx_drives:
            return out
        line = self.tx_line ^ (self.pau_mode >> 2 & 1)
        val = line << (self.out_base & 7)
        mask = 1 << (self.out_base & 7)
        if self.pau_mode >> 3 & 1:
            val |= (1 - line) << ((self.out_base + 1) & 7)
            mask |= 1 << ((self.out_base + 1) & 7)
        mask &= self.pin_mask
        return (out & ~mask) | (val & mask)

    @property
    def pins(self) -> int:
        """Pin values driven (standalone SM): latch, PAU TX, open drain."""
        return pad_outputs(self.bus, [self])[0]

    @property
    def pin_oe(self) -> int:
        """Pin output enables (standalone SM)."""
        return pad_outputs(self.bus, [self])[1]

    def load_program(self, program: Sequence[int], origin: int = 0) -> None:
        """Load instruction words and the configuration carried by an asm.Program."""
        for i, word in enumerate(program):
            if origin + i < IMEM_WORDS:
                self.memory[origin + i] = word & 0xFFFF
        if origin == 0:
            self.wrap_bottom = getattr(program, "wrap_bottom", 0)
            self.wrap_top = getattr(program, "wrap_top", max(0, len(program) - 1))
        for attr in ("side_set_count", "side_set_opt", "side_set_pindirs", "push_thresh",
                     "pull_thresh", "pau_mode", "crc_cfg", "crc_poly", "crc_res"):
            if hasattr(program, attr):
                setattr(self, attr, getattr(program, attr))
        if hasattr(program, "cond_cfg"):
            self.cond_cfg = list(program.cond_cfg)
        if getattr(program, "_has_explicit_open_drain", False) or getattr(program, "open_drain_mask", 0):
            self.bus.od_mask = program.open_drain_mask & 0xFF
        if getattr(program, "_has_explicit_clkdiv", False):
            self.clkdiv_cfg_int = program.clkdiv_int or 1
            self.clkdiv_cfg_frac = program.clkdiv_frac & 0xFF

    def reset(self, pc: int = 0) -> None:
        """CTRL.RESET: SM registers, FIFOs, PAU; COND = COND_CFG, PAU_BAUD = PAU_MODE.BAUD."""
        self.x = self.y = self.isr = self.osr = 0
        self.isr_count = 0
        self.osr_count = 16                             # OSR empty: autopull loads first
        self.pc = pc % IMEM_WORDS
        self.delay_counter = 0
        self.stalled = False
        self.stall_reason = ""
        self.stall_info: dict = {}
        self._stage_next_pc = 0
        self._current_instr_str = "nop"
        self.tx_fifo.clear()
        self.rx_fifo.clear()
        self.tx_back.clear()
        self.rx_back.clear()
        self.tx_flight = None
        self.prev_gpio_in = self.gpio_in
        self.timeout_val = self.timeout_counter = 0
        self.timeout_flag = False
        self.cond = list(self.cond_cfg)
        self.clkdiv_int, self._clkdiv_frac = self.clkdiv_cfg_int, self.clkdiv_cfg_frac
        self.dlymul = 1
        self.clkdiv_counter = self.clkdiv_accum = self._current_clkdiv_period = 0
        self.cycle_count = 0
        self.history.clear()
        # PAU
        self.pau_ctl = 0
        self.pau_baud = (self.pau_mode >> 10) & 0x1F
        self.crc = 0
        self.tx_queue: deque[tuple[int, bool, bool]] = deque()   # (bit, stuffed, monitored)
        self.tx_monitor = False                         # the bit on the line is monitored
        self.tx_bit_err = False
        self.tx_line = 0
        self.tx_active = False
        self.tx_half = 0
        self.tx_cnt = 0
        self.tx_bit = 0
        self.tx_stuff = _Stuffer()
        self.tx_stuff_pending: int | None = None
        self.rx_queue: deque[int] = deque()
        self.rx_prev_in = 0
        self.rx_armed = True
        self.rx_cnt = 0
        self.rx_since = 0
        self.rx_quiet = 0
        self.rx_mid = False
        self.rx_last_sample = 0
        self.rx_stuff = _Stuffer()
        self.rx_stuff_err = False
        self.rx_overrun = False
        self.cond_prev = [self._cond_value(s) for s in range(4)]

    # ------------------------------------------------------------------ FIFOs (host side)

    def tx_room(self) -> bool:
        return (not self.tx_back and self.tx_flight is None and len(self.tx_fifo) < self.fifo_depth
                or self.buffer and len(self.tx_back) < BUFFER_WORDS)

    def tx_write(self, word: int) -> bool:
        """Write a TX word as the host does: into the front if nothing waits in the SRAM part."""
        if not self.tx_back and self.tx_flight is None and len(self.tx_fifo) < self.fifo_depth:
            self.tx_fifo.append(word & 0xFFFF)
        elif self.buffer and len(self.tx_back) < BUFFER_WORDS:
            self.tx_back.append(word & 0xFFFF)
        else:
            return False
        return True

    def rx_read(self) -> int | None:
        """Read an RX word as the host does: the SRAM part first (older), then the front."""
        if self.rx_back:
            return self.rx_back.popleft()
        return self.rx_fifo.popleft() if self.rx_fifo else None

    @property
    def tx_level(self) -> int:
        return len(self.tx_fifo) + len(self.tx_back) + (self.tx_flight is not None)

    @property
    def rx_level(self) -> int:
        return len(self.rx_fifo) + len(self.rx_back)

    def buffer_clock(self) -> None:
        """End of a clock: the data SRAM moves one word (ISA.md, FIFOs)."""
        if not self.buffer:
            return
        if self.tx_flight is not None:
            self.tx_fifo.append(self.tx_flight)
            self.tx_flight = None
        if self.tx_back and len(self.tx_fifo) < self.fifo_depth:
            self.tx_flight = self.tx_back.popleft()
        elif self.rx_fifo and len(self.rx_back) < BUFFER_WORDS:
            self.rx_back.append(self.rx_fifo.popleft())

    # ------------------------------------------------------------------ PAU

    @property
    def _half(self) -> int:
        return self.pau_baud or 32

    @property
    def _crc_width(self) -> int:
        return (self.crc_cfg & 0x1F) + 1

    @property
    def _crc_mask(self) -> int:
        return (1 << self._crc_width) - 1

    def _crc_feed(self, bit: int) -> None:
        w, mask = self._crc_width, self._crc_mask
        fb = ((self.crc >> (w - 1)) & 1) ^ bit
        self.crc = (self.crc << 1) & mask
        if fb:
            self.crc ^= self.crc_poly & mask

    def _stuff_cfg(self) -> tuple[bool, int, int]:
        on = bool(self.pau_ctl & 4)
        return on, (self.pau_mode >> 8) & 3, ((self.pau_mode >> 4) & 0xF) or 16

    def _set_pau_ctl(self, val: int) -> None:
        draining = self.tx_drives
        old, self.pau_ctl = self.pau_ctl, val & 0x1F
        if val & 1 and not old & 1:
            self.tx_bit_err = False
        if val & 1 and not old & 1 and not draining:
            self.tx_line = ((self.gpio_out >> (self.out_base & 7)) & 1) ^ (self.pau_mode >> 2 & 1)
            self.tx_stuff = _Stuffer()
            self.tx_stuff_pending = None
        if val & 2 and not old & 2:
            self.rx_queue.clear()
            self.rx_armed = True
            self.rx_mid = False
            self.rx_cnt = self.rx_since = self.rx_quiet = 0
            self.rx_prev_in = self.rx_last_sample = self._rx_input()
            self.rx_stuff = _Stuffer()
            self.rx_stuff_err = self.rx_overrun = False
        if not val & 4:
            self.rx_stuff = _Stuffer()

    def _tx_start_bit(self) -> None:
        """Start the next bit (a stuffed one first) and drive its first half."""
        _, mode, length = self._stuff_cfg()
        if self.tx_stuff_pending is not None:
            b = self.tx_stuff_pending                   # monitored like the bit before it
            self.tx_stuff_pending = None
            self.tx_stuff.stuffed(b, mode)
        else:
            b, stuffed, self.tx_monitor = self.tx_queue.popleft()
            if stuffed:
                self.tx_stuff.count(b, mode)
                self.tx_stuff_pending = self.tx_stuff.due(mode, length)
            else:
                self.tx_stuff = _Stuffer()
        self.tx_bit, self.tx_half, self.tx_cnt, self.tx_active = b, 0, self._half, True
        enc = self.pau_mode & 3
        if enc == 0:
            self.tx_line = b
        elif enc == 1:
            self.tx_line ^= 1 - b
        elif enc == 2:
            self.tx_line = 1 - b
        else:
            self.tx_line ^= 1

    def _pau_tx_strobe(self) -> None:
        if not self.tx_drives:
            return
        if self.tx_active:
            self.tx_cnt -= 1
            if self.tx_cnt:
                return
            if self.tx_half == 0:                       # middle of the bit
                if self.tx_monitor and self._rx_input() != self.tx_line:
                    self.tx_bit_err = True              # quiet until TX is turned on again
                    self.tx_queue.clear()
                    self.tx_active = False
                    self.tx_stuff_pending = None
                    return
                self.tx_half, self.tx_cnt = 1, self._half
                enc = self.pau_mode & 3
                if enc == 2:
                    self.tx_line = self.tx_bit
                elif enc == 3 and self.tx_bit:
                    self.tx_line ^= 1
                return
            self.tx_active = False                      # end of the bit
        if self.tx_queue or self.tx_stuff_pending is not None:
            self._tx_start_bit()

    def _tx_abort(self) -> None:
        self.tx_queue.clear()
        self.tx_active = False
        self.tx_stuff_pending = None
        self.pau_ctl &= ~1

    def _rx_input(self) -> int:
        return ((self.gpio_in >> (self.in_base & 7)) & 1) ^ (self.pau_mode >> 2 & 1)

    def _rx_bit(self, b: int) -> None:
        """A decoded bit: destuff, CRC, queue."""
        on, mode, length = self._stuff_cfg()
        if on:
            due = self.rx_stuff.due(mode, length)
            if due is not None:
                if b == due:
                    self.rx_stuff.stuffed(b, mode)
                    return
                self.rx_stuff_err = True
            self.rx_stuff.count(b, mode)
        if self.pau_ctl & 8 and not self.pau_ctl & 1:
            self._crc_feed(b)
        if len(self.rx_queue) >= PAU_QUEUE_BITS:
            self.rx_overrun = True
        else:
            self.rx_queue.append(b)

    def _pau_rx_strobe(self) -> None:
        if not self.pau_ctl & 2:
            return
        inp = self._rx_input()
        if self.pau_mode >> 3 & 1 and self._pau_status(8):   # DIFF: SE0 ends the frame
            self.rx_prev_in, self.rx_armed, self.rx_quiet = inp, True, 0
            return
        edge = inp != self.rx_prev_in
        rising = edge and inp == 1
        self.rx_prev_in = inp
        self.rx_quiet = 0 if edge else self.rx_quiet + 1
        h = self._half
        enc = self.pau_mode & 3
        if enc < 2:                                     # NRZ / NRZI: sample mid-bit
            if edge:
                self.rx_armed, self.rx_cnt = False, h
                return
            if self.rx_armed:
                return
            self.rx_cnt -= 1
            if self.rx_cnt:
                return
            self.rx_cnt = 2 * h
            if enc == 0:
                self._rx_bit(inp)
            else:
                self._rx_bit(1 if inp == self.rx_last_sample else 0)
            self.rx_last_sample = inp
            return
        if self.rx_armed:                               # Manchester / biphase mark: edge timing
            if edge:
                self.rx_armed, self.rx_since, self.rx_mid = False, 0, False
                if enc == 2:
                    self._rx_bit(1 if rising else 0)
            return
        self.rx_since += 1
        t2 = 2 * self.rx_since                          # compare in half-H units
        if edge:
            if enc == 2:
                if 3 * h <= t2 <= 5 * h:
                    self._rx_bit(1 if rising else 0)
                    self.rx_since = 0
            elif h <= t2 < 3 * h:
                self.rx_mid = True
            elif 3 * h <= t2 <= 5 * h:
                self._rx_bit(1 if self.rx_mid else 0)
                self.rx_since, self.rx_mid = 0, False
            return
        if t2 > 5 * h:                                  # no edge in time: end of frame
            if enc == 3:
                self._rx_bit(1 if self.rx_mid else 0)   # the last bit has no closing boundary
            self.rx_armed = True

    def _pau_status(self, idx: int) -> int:
        if idx == 0:
            return int(not self.tx_active and not self.tx_queue and self.tx_stuff_pending is None)
        if idx == 1:
            return int(not self.pau_ctl & 2 or self.rx_quiet >= 4 * self._half)
        if idx == 2:
            return int(len(self.tx_queue) <= PAU_QUEUE_BITS - 16)
        if idx == 3:
            return int(bool(self.rx_queue))
        if idx == 4:
            return int(self.crc == self.crc_res & self._crc_mask)
        if idx == 5:
            return int(self.crc == 0)
        if idx == 6:
            return int(self.rx_stuff_err)
        if idx == 7:
            return int(self.rx_overrun)
        pair = (self.gpio_in >> (self.in_base & 7)) & 1, (self.gpio_in >> ((self.in_base + 1) & 7)) & 1
        if idx == 8:
            return int(pair == (0, 0))
        if idx == 9:
            return int(pair == (1, 1))
        if idx == 10:
            return int(self.tx_bit_err)
        return 0

    # ------------------------------------------------------------------ conditions

    def _sm_status(self, idx: int) -> int:
        return int([
            self.x == 0, self.y == 0, self.x == self.y, self.timeout_flag,
            self.osr_count >= (self.pull_thresh or 16), self.isr_count >= (self.push_thresh or 16),
            self.tx_level == 0,
            len(self.rx_fifo) >= self.fifo_depth and (not self.buffer or len(self.rx_back) >= BUFFER_WORDS),
        ][idx] if idx < 8 else 0)

    def status_word(self) -> int:
        """The STATUS operand: SM status [7:0], PAU status [15:8] (ISA.md)."""
        return sum(self._sm_status(i) << i | self._pau_status(i) << (8 + i) for i in range(8))

    def _cond_value(self, slot: int) -> int:
        c = self.cond[slot]
        src, idx = (c >> 4) & 7, c & 0xF
        if src == CS_GPIO:
            v = (self.gpio_in >> (idx & 7)) & 1
        elif src == CS_INBASE:
            v = (self.gpio_in >> ((self.in_base + idx) & 7)) & 1
        elif src == CS_IRQ:
            v = int(self.irq_flags[idx & 7])
        elif src == CS_PEER:
            v = int(self.peer_irq_flags[idx & 7])
        elif src == CS_XBIT:
            v = (self.x >> idx) & 1
        elif src == CS_YBIT:
            v = (self.y >> idx) & 1
        elif src == CS_STATUS:
            v = self._sm_status(idx)
        else:
            v = self._pau_status(idx)
        return v ^ (c >> 7)

    def _wait_met(self, pol: int, edge: int, slot: int) -> bool:
        cur = self._cond_value(slot)
        if not edge:
            return cur == pol
        return self.cond_prev[slot] == 1 - pol and cur == pol

    # ------------------------------------------------------------------ pins

    def _write_pins(self, mask: int, value: int) -> None:
        self.bus.write(self.index, 0, mask & self.pin_mask, value)

    def _write_dirs(self, mask: int, value: int) -> None:
        self.bus.write(self.index, 1, mask & self.pin_mask, value)

    def _decode_delay_sideset(self, delay_ss: int) -> tuple[int, int | None]:
        if self.side_set_count == 0:
            return delay_ss & 0x1F, None
        delay_bits = 5 - self.side_set_count - (1 if self.side_set_opt else 0)
        delay = delay_ss & ((1 << delay_bits) - 1)
        ss_field = delay_ss >> delay_bits
        if self.side_set_opt and not (ss_field >> self.side_set_count) & 1:
            return delay, None
        return delay, ss_field & ((1 << self.side_set_count) - 1)

    def _apply_sideset(self, side_val: int | None) -> None:
        if side_val is None or self.side_set_count == 0:
            return
        mask = ((1 << self.side_set_count) - 1) << self.sideset_base
        val = (side_val << self.sideset_base) & mask & 0xFF
        if self.side_set_pindirs:
            self._write_dirs(mask, val)
        else:
            self._write_pins(mask, val)

    # ------------------------------------------------------------------ execution

    def _end_strobe(self, trace_pc: int) -> None:
        self.cond_prev = [self._cond_value(s) for s in range(4)]
        self.prev_gpio_in = self.gpio_in & 0xFF
        self.history.append(self._format_trace_row(trace_pc, self._current_instr_str))

    def step(self) -> None:
        """Advance the state machine by exactly one clock cycle."""
        self.strobed = False
        self._clock()
        if self._own_bus:
            self.bus.commit()
            self.buffer_clock()

    def _clock(self) -> None:
        self.cycle_count += 1
        trace_pc = self.pc

        # Fractional prescaler: only every Nth clock is a strobe
        if self.clkdiv_int > 1 or self._clkdiv_frac > 0:
            if self.clkdiv_counter == 0:
                self.clkdiv_accum += self._clkdiv_frac
                carry = int(self.clkdiv_accum >= 256)
                self.clkdiv_accum -= 256 * carry
                self._current_clkdiv_period = self.clkdiv_int + carry
            self.clkdiv_counter += 1
            if self.clkdiv_counter < self._current_clkdiv_period:
                self.history.append(self._format_trace_row(trace_pc, self._current_instr_str))
                return
            self.clkdiv_counter = 0

        self.strobed = True
        self._pau_tx_strobe()
        self._pau_rx_strobe()

        if self.delay_counter > 0:
            self.delay_counter -= 1
            if self.delay_counter == 0:
                self.pc = self._stage_next_pc
            self._end_strobe(trace_pc)
            return

        if self.stalled:
            if self.stall_reason == "RETRY":            # blocked without effect: issue again
                self.stalled = False
            else:
                self._stall_strobe()
                self._end_strobe(trace_pc)
                return

        self._execute()
        self._end_strobe(trace_pc)

    def _finish(self, delay: int, next_pc: int) -> None:
        if delay > 0:
            self.delay_counter = delay
            self._stage_next_pc = next_pc
        else:
            self.pc = next_pc

    def _stall_strobe(self) -> None:
        info, done = self.stall_info, False
        if self.stall_reason == "WAIT":
            if info["t_en"]:
                if self.timeout_counter > 0:
                    self.timeout_counter -= 1
                    self.timeout_val = self.timeout_counter
                if self.timeout_counter == 0:
                    self.timeout_val = 0
                    self.timeout_flag = True
                    done = True
            done = done or self._wait_met(info["pol"], info["edge"], info["slot"])
        elif self.stall_reason == "PULL":
            word = self._pull_word(info["peer"])
            if word is not None:
                self.osr, self.osr_count, done = word, 0, True
        elif self.stall_reason == "PUSH":
            if self._push_word(info["peer"]):
                self.isr = self.isr_count = 0
                done = True
        elif self.stall_reason == "IRQ":
            flags = self.peer_irq_flags if info["peer"] else self.irq_flags
            done = not flags[info["idx"]]
        if done:
            self.stalled = False
            self.stall_reason = ""
            self._finish(info["delay"], info["next_pc"])

    def _pull_word(self, peer: bool) -> int | None:
        """PULL source: the own TX front, or the peer's RX FIFO read as the host does."""
        if not peer:
            return self.tx_fifo.popleft() if self.tx_fifo else None
        if self.peer is not None:
            return self.peer.rx_read()
        return self.peer_rx_fifo.popleft() if self.peer_rx_fifo else None

    def _push_word(self, peer: bool) -> bool:
        """PUSH target: the own RX front, or the peer's TX FIFO written as the host does."""
        if not peer:
            if len(self.rx_fifo) >= self.fifo_depth:
                return False
            self.rx_fifo.append(self.isr)
            return True
        if self.peer is not None:
            return self.peer.tx_write(self.isr)
        if len(self.peer_tx_fifo) >= self.fifo_depth:
            return False
        self.peer_tx_fifo.append(self.isr)
        return True

    def _stall(self, reason: str, **info) -> None:
        self.stalled = True
        self.stall_reason = reason
        self.stall_info = info

    def _operand(self, code: int) -> int:
        """Source operand value (IN / ALU); PINDIRS reads 0 for IN (handled there)."""
        if code == R_SPECIAL:
            return self.status_word()
        return {R_PINS: self.gpio_in, R_PINDIRS: self.pindirs, R_X: self.x, R_Y: self.y,
                R_ISR: self.isr, R_OSR: self.osr}.get(code, 0)

    def _execute(self) -> None:
        word = self.memory[self.pc]
        self._current_instr_str = disassemble(word, self.side_set_count, self.side_set_opt)
        opcode, args = word >> 13, word & 0xFF
        delay, side_val = self._decode_delay_sideset((word >> 8) & 0x1F)
        self._apply_sideset(side_val)
        delay *= self.dlymul

        next_pc = self.wrap_bottom if self.pc == self.wrap_top else (self.pc + 1) % IMEM_WORDS

        if opcode >> 1 == OP_JMP >> 1:
            cond = (args >> 5) & 7
            if cond == JC_ALWAYS:
                taken = True
            elif cond == JC_NEVER:
                taken = False
            elif cond == JC_XDEC:
                taken, self.x = self.x != 0, (self.x - 1) & 0xFFFF
            elif cond == JC_YDEC:
                taken, self.y = self.y != 0, (self.y - 1) & 0xFFFF
            else:
                taken = bool(self._cond_value(cond - 4))
            if taken:
                next_pc = ((opcode & 1) << 5) | (args & 0x1F)

        elif opcode == OP_IN:
            src, auto = (args >> 5) & 7, (args >> 4) & 1
            count = (args & 0xF) or 16
            mask = (1 << count) - 1
            if src == R_PINS and self.pau_ctl & 2:
                if len(self.rx_queue) < count:
                    return self._stall("RETRY")
                bits = [self.rx_queue.popleft() for _ in range(count)]
                if not self.shift_in_right:
                    bits.reverse()
                val = sum(b << i for i, b in enumerate(bits))
            elif src == R_PINS:
                val = (self.gpio_in >> (self.in_base & 7)) & mask
            elif src == R_PINDIRS:
                val = 0
            else:
                val = self._operand(src) & mask
            if self.shift_in_right:
                self.isr = ((self.isr >> count) | (val << (16 - count))) & 0xFFFF
            else:
                self.isr = ((self.isr << count) | val) & 0xFFFF
            self.isr_count = min(16, self.isr_count + count)
            if auto and self.isr_count >= (self.push_thresh or 16):
                if self._push_word(False):
                    self.isr = self.isr_count = 0
                else:
                    return self._stall("PUSH", peer=False, delay=delay, next_pc=next_pc)

        elif opcode == OP_OUT:
            dest, auto = (args >> 5) & 7, (args >> 4) & 1
            count = (args & 0xF) or 16
            mask = (1 << count) - 1
            pull_at = self.pull_thresh or 16
            if auto and self.osr_count >= pull_at:
                if self.tx_fifo:
                    self.osr, self.osr_count = self.tx_fifo.popleft(), 0
                return self._stall("RETRY")
            if dest == R_PINS and self.pau_ctl & 1 and PAU_QUEUE_BITS - len(self.tx_queue) < count:
                return self._stall("RETRY")
            if self.shift_out_right:
                val = self.osr & mask
                self.osr = (self.osr >> count) & 0xFFFF
            else:
                val = (self.osr >> (16 - count)) & mask
                self.osr = (self.osr << count) & 0xFFFF
            self.osr_count = min(16, self.osr_count + count)
            if auto and self.osr_count >= pull_at and self.tx_fifo:
                self.osr, self.osr_count = self.tx_fifo.popleft(), 0
            if dest == R_PINS and self.pau_ctl & 1:
                order = range(count) if self.shift_out_right else range(count - 1, -1, -1)
                for i in order if not self.tx_bit_err else ():
                    b = (val >> i) & 1
                    self.tx_queue.append((b, bool(self.pau_ctl & 4), bool(self.pau_ctl & 16)))
                    if self.pau_ctl & 8:
                        self._crc_feed(b)
            elif dest == R_PINS:
                self._write_pins((mask << (self.out_base & 7)) & 0xFF, val << (self.out_base & 7))
            elif dest == R_PINDIRS:
                self._write_dirs(mask << (self.out_base & 7), val << (self.out_base & 7))
            elif dest == R_X:
                self.x = val
            elif dest == R_Y:
                self.y = val
            elif dest == R_ISR:
                self.isr, self.isr_count = val, count
            elif dest == R_SPECIAL:
                next_pc = val % IMEM_WORDS

        elif opcode >> 1 == OP_ALU >> 1:
            op = ((opcode & 1) << 2) | ((args >> 3) & 3)
            dest, src = (args >> 5) & 7, args & 7
            s = self._operand(src)
            d = {R_PINS: self.gpio_out, R_PINDIRS: self.pindirs, R_X: self.x, R_Y: self.y,
                 R_ISR: self.isr, R_OSR: self.osr}.get(dest, 0)
            res = [s, _rev16(s), d & s, d | s, d ^ s, ~(d ^ s), d + s, d - s][op] & 0xFFFF
            if dest == R_PINS:
                self._write_pins(0xFF, res)
            elif dest == R_PINDIRS:
                self._write_dirs(0xFF, res)
            elif dest == R_X:
                self.x = res
            elif dest == R_Y:
                self.y = res
            elif dest == R_ISR:
                self.isr, self.isr_count = res, 16
            elif dest == R_OSR:
                self.osr, self.osr_count = res, 0
            elif dest == R_SPECIAL:
                self.cond[(res >> 8) & 3] = res & 0xFF

        elif opcode == OP_CTRL:
            ctype, a = args >> 6, args & 0x3F
            if ctype == CT_IRQ:
                clear, wait, peer, idx = a >> 5 & 1, a >> 4 & 1, a >> 3 & 1, a & 7
                flags = self.peer_irq_flags if peer else self.irq_flags
                flags[idx] = not clear
                if wait and not clear:
                    return self._stall("IRQ", peer=peer, idx=idx, delay=delay, next_pc=next_pc)
            elif ctype == CT_FIFO and not a & 3:
                is_pull, block, ifcond, peer = a >> 5 & 1, a >> 4 & 1, a >> 3 & 1, a >> 2 & 1
                if is_pull:
                    if ifcond and self.osr_count < (self.pull_thresh or 16):
                        pass
                    elif (word := self._pull_word(peer)) is not None:
                        self.osr, self.osr_count = word, 0
                    elif not block:
                        self.osr, self.osr_count = self.x, 0
                    else:
                        return self._stall("PULL", peer=peer, delay=delay, next_pc=next_pc)
                else:
                    if ifcond and self.isr_count < (self.push_thresh or 16):
                        pass
                    elif self._push_word(peer):
                        self.isr = self.isr_count = 0
                    elif not block:
                        self.isr = self.isr_count = 0
                    else:
                        return self._stall("PUSH", peer=peer, delay=delay, next_pc=next_pc)
            elif ctype == CT_WAIT and not a & 4:
                pol, edge, t_en, slot = a >> 5 & 1, a >> 4 & 1, a >> 3 & 1, a & 3
                if t_en:
                    self.timeout_counter = self.timeout_val
                    self.timeout_flag = False
                if not self._wait_met(pol, edge, slot):
                    return self._stall("WAIT", pol=pol, edge=edge, t_en=t_en, slot=slot,
                                       delay=delay, next_pc=next_pc)
            elif ctype == CT_PAU and (a >> 4 == 3 or not a & 0xF):
                action = a >> 4
                if action == 3:                         # CRC feed: last bits into the ISR, oldest first
                    n = (a & 0xF) or 16
                    order = range(16 - n, 16) if self.shift_in_right else range(n - 1, -1, -1)
                    for i in order:
                        self._crc_feed(self.isr >> i & 1)
                elif action == 0:
                    self.crc = self._crc_mask if self.crc_cfg >> 5 & 1 else 0
                elif action == 2:
                    self._tx_abort()
                elif action == 1:
                    w = self._crc_width
                    if PAU_QUEUE_BITS - len(self.tx_queue) < w:
                        return self._stall("RETRY")
                    out = self.crc ^ (self._crc_mask if self.crc_cfg >> 6 & 1 else 0)
                    if not self.tx_bit_err:
                        self.tx_queue.extend(((out >> i) & 1, bool(self.pau_ctl & 4), bool(self.pau_ctl & 16))
                                             for i in range(w - 1, -1, -1))

        else:  # SET
            dest, val = (args >> 5) & 7, args & 0x1F
            if dest in (0, 1):
                m = (((1 << self.set_count) - 1) & 0x1F) << (self.set_base & 7)
                (self._write_dirs if dest else self._write_pins)(m, val << (self.set_base & 7))
            elif dest == 2:
                self.x = val
            elif dest == 3:
                self.y = val
            elif dest == 4:
                self.dlymul = val or 1
            elif dest == 5:
                self.timeout_val = self.timeout_counter = val
                self.timeout_flag = False
            elif dest == 6:
                self.pau_baud = val
            else:
                self._set_pau_ctl(val)

        self._finish(delay, next_pc)

    # ------------------------------------------------------------------ traces

    def _format_trace_row(self, pc: int, instr_str: str) -> str:
        return (f"{self.cycle_count:4d} | {pc:2d} | {instr_str:<18} | {self.delay_counter:3d} | "
                f"0x{self.x:04x} | 0x{self.y:04x} | 0x{self.osr:04x} | 0x{self.isr:04x} | "
                f"  0x{self.gpio_in:02x} |    0x{self.pins:02x} | {'1' if self.stalled else '0':^5}")

    def get_trace_row(self, cycle: int | None = None) -> str:
        """Most recent trace row (or the current state)."""
        if self.history:
            return self.history[-1]
        instr = disassemble(self.memory[self.pc], self.side_set_count, self.side_set_opt)
        return self._format_trace_row(self.pc, instr)

    def get_trace_table(self, last_n: int = 15) -> str:
        """Compact rolling window trace table."""
        return "\n".join([HEADER, SEPARATOR] + list(self.history)[-last_n:])

    def run(self, max_cycles: int = 1000) -> int:
        """Run until stalled or max_cycles reached. Returns cycles run."""
        for _ in range(max_cycles):
            self.step()
            if self.stalled:
                break
        return self.cycle_count


def main() -> None:
    """CLI entry point for emu.py."""
    parser = argparse.ArgumentParser(description="openPIO emulator (ISA v2)")
    parser.add_argument("input", help="Binary (.bin) or Assembly (.asm) file")
    parser.add_argument("-c", "--cycles", type=int, default=32, help="Number of cycles to run")
    args = parser.parse_args()
    if args.input.endswith(".asm"):
        with open(args.input, "r", encoding="utf-8") as f:
            prog = asm.assemble(f.read())
    else:
        with open(args.input, "rb") as f:
            data = f.read()
        prog = list(struct.unpack(f"<{len(data) // 2}H", data[:len(data) // 2 * 2]))
    sm = StateMachine(prog)
    for _ in range(args.cycles):
        sm.step()
    print(sm.get_trace_table(args.cycles))


if __name__ == "__main__":
    main()
