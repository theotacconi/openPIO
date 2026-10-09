"""emu.py - Cycle-Accurate Minimal Emulator for 16-bit Custom PIO ASIC Architecture.

Based on ISA.md specification.
Executes 16-bit binary instructions cycle-by-cycle against the ISA semantics
and produces concise, LLM-optimized tabular traces.
"""

from __future__ import annotations

import argparse
import collections
from collections import deque
import struct
import sys
from typing import Sequence

import asm
from asm import (
    OP_ALU,
    OP_IN,
    OP_IRQ,
    OP_JMP,
    OP_OUT,
    OP_PUSH,
    OP_SET,
    OP_WAIT,
    disassemble,
)

HEADER = "CYC  | PC | INSTR              | DLY | X      | Y      | OSR    | ISR    | PIN_IN | PIN_OUT | STALL"
SEPARATOR = "-----+----+--------------------+-----+--------+--------+--------+--------+--------+---------+------"


class StateMachine:
    """Cycle-accurate emulator for a single PIO state machine."""

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
        jmp_pin: int = 0,
        instructions: Sequence[int] | None = None,
    ) -> None:
        if program is None and instructions is not None:
            program = instructions
        # Instruction memory: 32 16-bit words
        self.memory: list[int] = [0] * 32

        # Wrap configuration
        self.wrap_bottom: int = wrap_bottom
        self.wrap_top: int = wrap_top if wrap_top is not None else 31

        # Side-set configuration
        self.side_set_count: int = side_set_count
        self.side_set_opt: bool = side_set_opt
        self.side_set_pindirs: bool = side_set_pindirs

        # Open-drain IO control interface (RFC-I2C-2)
        self._open_drain_mask: int = open_drain_mask & 0xFF

        # Clock divider & 8-bit fractional prescaler (RFC-04, RFC-UART-1)
        self.clkdiv_int: int = clkdiv_int if clkdiv_int > 0 else 1
        self._clkdiv_frac: int = clkdiv_frac & 0xFF
        self.clkdiv_counter: int = 0
        self.clkdiv_accum: int = 0
        self._current_clkdiv_period: int = 0

        # PAU execution datapath states (RFC-01, RFC-02)
        self.pau_nrzi_state: int = 1  # 1 = J-state (D-=1, D+=0), 2 = K-state (D-=0, D+=1)
        self.pau_ones_count: int = 0  # Consecutive ones counter for bit-stuffing
        self.pau_tx_queue: deque[int] = deque()  # Output pin queue for Manchester / PAU modulator

        # Load program if provided
        if program is not None:
            self.load_program(program)

        # 16-bit registers
        self.x: int = 0
        self.y: int = 0
        self.isr: int = 0
        self.osr: int = 0

        # Shift bit counters (0-16)
        self.isr_count: int = 0
        self.osr_count: int = 0
        self.shift_in_right: bool = True
        self.shift_out_right: bool = True

        # Control registers
        self.pc: int = 0
        self.delay_counter: int = 0
        self.stalled: bool = False

        # Internal control state
        self._stage_next_pc: int = 0
        self._current_instr_str: str = "nop"
        self.stall_reason: str = ""
        self.stall_info: dict = {}

        # FIFOs
        self.fifo_depth: int = fifo_depth
        self.tx_fifo: deque[int] = deque(maxlen=fifo_depth)
        self.rx_fifo: deque[int] = deque(maxlen=fifo_depth)

        # Pins (8-bit)
        self.gpio_in: int = 0
        self.gpio_out: int = 0
        self.pindirs: int = 0
        self.prev_gpio_in: int = 0

        # Pin mapping configurations
        self.in_base: int = in_base
        self.out_base: int = out_base
        self.set_base: int = set_base
        self.set_count: int = set_count
        self.sideset_base: int = sideset_base
        self.jmp_pin: int = jmp_pin

        # Guard timeout & IRQ flags
        self.timeout_val: int = 0
        self.timeout_counter: int = 0
        self.timeout_flag: bool = False
        self.irq_flags: list[bool] = [False] * 8
        self.peer_irq_flags: list[bool] = [False] * 8

        # PAU config registers
        self.pau_baud: int = 0
        self.pau_cfg: int = 0

        # Execution tracking & LLM-optimized trace rolling buffer
        self.cycle_count: int = 0
        self.history: deque[str] = deque(maxlen=15)

    # Upper-case properties for ISA register naming conventions
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
    def ISR(self) -> int:
        return self.isr

    @ISR.setter
    def ISR(self, val: int) -> None:
        self.isr = val & 0xFFFF

    @property
    def OSR(self) -> int:
        return self.osr

    @OSR.setter
    def OSR(self, val: int) -> None:
        self.osr = val & 0xFFFF

    @property
    def PC(self) -> int:
        return self.pc

    @PC.setter
    def PC(self, val: int) -> None:
        self.pc = val & 0x1F

    @property
    def wrap_target(self) -> int:
        return self.wrap_bottom

    @wrap_target.setter
    def wrap_target(self, val: int) -> None:
        self.wrap_bottom = val & 0x1F

    @property
    def wrap(self) -> int:
        return self.wrap_top

    @wrap.setter
    def wrap(self, val: int) -> None:
        self.wrap_top = val & 0x1F

    @property
    def open_drain_mask(self) -> int:
        return self._open_drain_mask

    @open_drain_mask.setter
    def open_drain_mask(self, val: int) -> None:
        self._open_drain_mask = val & 0xFF

    @property
    def clkdiv(self) -> int:
        return self.clkdiv_int

    @clkdiv.setter
    def clkdiv(self, val: int) -> None:
        self.clkdiv_int = val if val > 0 else 1

    @property
    def clkdiv_frac(self) -> int:
        return self._clkdiv_frac

    @clkdiv_frac.setter
    def clkdiv_frac(self, val: int) -> None:
        self._clkdiv_frac = val & 0xFF

    def _apply_open_drain(self) -> None:
        """Apply open-drain IO behavior: when open_drain_mask bit is set,
        driving 1 sets pindirs to 0 (Hi-Z float), driving 0 sets pindirs to 1 (active low drive)."""
        if not self._open_drain_mask:
            return
        for pin in range(8):
            if (self._open_drain_mask >> pin) & 1:
                bit = (self.gpio_out >> pin) & 1
                if bit == 1:
                    self.pindirs &= ~(1 << pin)
                else:
                    self.pindirs |= (1 << pin)
                    self.gpio_out &= ~(1 << pin)

    def load_program(self, program: Sequence[int], origin: int = 0) -> None:
        """Load assembled instruction words into memory."""
        for i, word in enumerate(program):
            if origin + i < 32:
                self.memory[origin + i] = word & 0xFFFF

        # If program carries assembler metadata, apply it
        if hasattr(program, "wrap_bottom"):
            self.wrap_bottom = getattr(program, "wrap_bottom")
        elif origin == 0:
            self.wrap_bottom = 0

        if hasattr(program, "wrap_top"):
            self.wrap_top = getattr(program, "wrap_top")
        elif origin == 0:
            self.wrap_top = max(0, len(program) - 1)

        if hasattr(program, "side_set_count"):
            self.side_set_count = getattr(program, "side_set_count")
        if hasattr(program, "side_set_opt"):
            self.side_set_opt = getattr(program, "side_set_opt")
        if hasattr(program, "side_set_pindirs"):
            self.side_set_pindirs = getattr(program, "side_set_pindirs")

        # Open-drain IO control interface (RFC-I2C-2)
        if getattr(program, "_has_explicit_open_drain", False):
            self._open_drain_mask = getattr(program, "open_drain_mask", 0) & 0xFF
        elif hasattr(program, "open_drain_mask") and getattr(program, "open_drain_mask") != 0:
            self._open_drain_mask = getattr(program, "open_drain_mask") & 0xFF

        # Fractional Clock Divider (RFC-UART-1)
        if getattr(program, "_has_explicit_clkdiv", False):
            if getattr(program, "clkdiv_int", None) is not None:
                self.clkdiv_int = getattr(program, "clkdiv_int")
            if getattr(program, "clkdiv_frac", None) is not None:
                self._clkdiv_frac = getattr(program, "clkdiv_frac") & 0xFF
        else:
            if hasattr(program, "clkdiv_int") and getattr(program, "clkdiv_int") > 1:
                self.clkdiv_int = getattr(program, "clkdiv_int")
            if hasattr(program, "clkdiv_frac") and getattr(program, "clkdiv_frac") != 0:
                self._clkdiv_frac = getattr(program, "clkdiv_frac") & 0xFF

    def reset(self, pc: int = 0) -> None:
        """Reset state machine registers, control flags, and FIFOs."""
        self.x = 0
        self.y = 0
        self.isr = 0
        self.osr = 0
        self.isr_count = 0
        self.osr_count = 0
        self.pc = pc & 0x1F
        self.delay_counter = 0
        self.stalled = False
        self.stall_reason = ""
        self.stall_info.clear()
        self.tx_fifo.clear()
        self.rx_fifo.clear()
        self.timeout_counter = 0
        self.timeout_flag = False
        self.cycle_count = 0
        self.history.clear()
        self.pau_nrzi_state = 1
        self.pau_ones_count = 0
        self.pau_tx_queue.clear()
        self.clkdiv_counter = 0
        self.clkdiv_accum = 0
        self._current_clkdiv_period = 0

    def _decode_delay_sideset(self, delay_ss: int) -> tuple[int, int | None]:
        """Decode the 5-bit Delay / Side-Set field."""
        if self.side_set_count == 0:
            return delay_ss & 0x1F, None

        total_ss_bits = self.side_set_count + (1 if self.side_set_opt else 0)
        delay_bits = 5 - total_ss_bits
        delay_mask = (1 << delay_bits) - 1
        delay = delay_ss & delay_mask
        ss_field = delay_ss >> delay_bits

        if self.side_set_opt:
            en_bit = (ss_field >> self.side_set_count) & 1
            if en_bit:
                return delay, ss_field & ((1 << self.side_set_count) - 1)
            return delay, None

        return delay, ss_field & ((1 << self.side_set_count) - 1)

    def _apply_sideset(self, side_val: int | None) -> None:
        """Apply side-set pin update to gpio_out or pindirs."""
        if side_val is None or self.side_set_count == 0:
            return
        mask = ((1 << self.side_set_count) - 1) << self.sideset_base
        if self.side_set_pindirs:
            self.pindirs = ((self.pindirs & ~mask) | ((side_val << self.sideset_base) & mask)) & 0xFF
        else:
            self.gpio_out = ((self.gpio_out & ~mask) | ((side_val << self.sideset_base) & mask)) & 0xFF
            self._apply_open_drain()

    def _evaluate_wait_condition(self, pol: int, edge: int, src: int, idx: int) -> bool:
        """Sample condition source and evaluate if WAIT is satisfied."""
        if src == 0:  # GPIO pin
            curr = (self.gpio_in >> (idx & 7)) & 1
            prev = (self.prev_gpio_in >> (idx & 7)) & 1
        elif src == 1:  # IN_BASE
            pin = (self.in_base + idx) & 7
            curr = (self.gpio_in >> pin) & 1
            prev = (self.prev_gpio_in >> pin) & 1
        elif src == 2:  # IRQ
            curr = 1 if self.irq_flags[idx & 7] else 0
            prev = curr
        elif src == 3:  # Peer SM IRQ
            curr = 1 if self.peer_irq_flags[idx & 7] else 0
            prev = curr
        else:
            return False

        if edge == 0:  # Level
            return curr == pol

        # Edge: pol=1 is rising (0->1), pol=0 is falling (1->0)
        if pol == 1:
            return prev == 0 and curr == 1
        return prev == 1 and curr == 0

    def step(self) -> None:
        """Advance the state machine by exactly one clock cycle."""
        self.cycle_count += 1
        trace_pc = self.pc

        # -------------------------------------------------------------
        # 0. Fractional Clock Prescaler (RFC-UART-1)
        # -------------------------------------------------------------
        if self.clkdiv_int > 1 or self._clkdiv_frac > 0:
            if self.clkdiv_counter == 0:
                self.clkdiv_accum += self._clkdiv_frac
                carry = 0
                if self.clkdiv_accum >= 256:
                    carry = 1
                    self.clkdiv_accum -= 256
                self._current_clkdiv_period = self.clkdiv_int + carry

            self.clkdiv_counter += 1
            if self.clkdiv_counter < self._current_clkdiv_period:
                # Sub-strobe cycle: SM execution paused this clock cycle
                self.prev_gpio_in = self.gpio_in & 0xFF
                row = self._format_trace_row(trace_pc, self._current_instr_str)
                self.history.append(row)
                return
            self.clkdiv_counter = 0

        # Advance PAU Manchester TX modulator queue if active
        if self.pau_tx_queue:
            next_bit = self.pau_tx_queue.popleft()
            mask_pin = 1 << (self.out_base & 7)
            self.gpio_out = ((self.gpio_out & ~mask_pin) | ((next_bit << (self.out_base & 7)) & mask_pin)) & 0xFF
            self._apply_open_drain()

        # -------------------------------------------------------------
        # 1. State Machine is Delaying
        # -------------------------------------------------------------
        if self.delay_counter > 0:
            self.delay_counter -= 1
            if self.delay_counter == 0:
                self.pc = self._stage_next_pc

            self.prev_gpio_in = self.gpio_in & 0xFF
            row = self._format_trace_row(trace_pc, self._current_instr_str)
            self.history.append(row)
            return

        # -------------------------------------------------------------
        # 2. State Machine is Stalled
        # -------------------------------------------------------------
        if self.stalled:
            un_stalled = False

            if self.stall_reason == "WAIT":
                t_en = self.stall_info.get("t_en", 0)
                if t_en:
                    if self.timeout_counter > 0:
                        self.timeout_counter -= 1
                        self.timeout_val = self.timeout_counter
                    if self.timeout_counter == 0:
                        self.timeout_val = 0
                        self.timeout_flag = True
                        un_stalled = True

                if not un_stalled:
                    pol = self.stall_info["pol"]
                    edge = self.stall_info["edge"]
                    src = self.stall_info["src"]
                    idx = self.stall_info["idx"]
                    if self._evaluate_wait_condition(pol, edge, src, idx):
                        un_stalled = True

            elif self.stall_reason == "PULL":
                if len(self.tx_fifo) > 0:
                    self.osr = self.tx_fifo.popleft() & 0xFFFF
                    self.osr_count = 0
                    un_stalled = True

            elif self.stall_reason == "PUSH":
                if len(self.rx_fifo) < self.fifo_depth:
                    self.rx_fifo.append(self.isr & 0xFFFF)
                    self.isr = 0
                    self.isr_count = 0
                    un_stalled = True

            elif self.stall_reason == "IRQ":
                idx = self.stall_info.get("idx", 0)
                peer = self.stall_info.get("peer", 0)
                flags = self.peer_irq_flags if peer else self.irq_flags
                if not flags[idx]:
                    un_stalled = True

            elif self.stall_reason == "PAU_TX":
                half_period = self.pau_baud if self.pau_baud > 0 else 1
                if len(self.pau_tx_queue) <= half_period * 2:
                    un_stalled = True

            elif self.stall_reason == "PAU_BUSY":
                if len(self.pau_tx_queue) == 0:
                    un_stalled = True

            if un_stalled:
                self.stalled = False
                delay = self.stall_info.get("delay", 0)
                next_pc = self.stall_info.get("next_pc", (self.pc + 1) & 0x1F)
                if self.stall_reason == "PAU_BUSY":
                    idx = self.stall_info.get("idx", 0)
                    peer = self.stall_info.get("peer", 0)
                    flags = self.peer_irq_flags if peer else self.irq_flags
                    flags[idx] = True
                    if self.stall_info.get("wait", 0):
                        self.stalled = True
                        self.stall_reason = "IRQ"
                        self.stall_info = {
                            "peer": peer,
                            "idx": idx,
                            "delay": delay,
                            "next_pc": next_pc,
                        }
                        self.prev_gpio_in = self.gpio_in & 0xFF
                        row = self._format_trace_row(trace_pc, self._current_instr_str)
                        self.history.append(row)
                        return
                self.stall_reason = ""
                self.stall_info.clear()

                if delay > 0:
                    self.delay_counter = delay
                    self._stage_next_pc = next_pc
                else:
                    self.pc = next_pc

                self.prev_gpio_in = self.gpio_in & 0xFF
                row = self._format_trace_row(trace_pc, self._current_instr_str)
                self.history.append(row)
                return
            else:
                self.prev_gpio_in = self.gpio_in & 0xFF
                row = self._format_trace_row(trace_pc, self._current_instr_str)
                self.history.append(row)
                return

        # -------------------------------------------------------------
        # 3. Normal Instruction Execution
        # -------------------------------------------------------------
        trace_pc = self.pc
        word = self.memory[self.pc] & 0xFFFF
        self._current_instr_str = disassemble(word, self.side_set_count, self.side_set_opt)

        opcode = (word >> 13) & 0x7
        delay_ss = (word >> 8) & 0x1F
        args = word & 0xFF

        delay, side_val = self._decode_delay_sideset(delay_ss)
        self._apply_sideset(side_val)

        # Default wrap handling
        if self.pc == self.wrap_top:
            default_next_pc = self.wrap_bottom
        else:
            default_next_pc = (self.pc + 1) & 0x1F

        next_pc = default_next_pc
        inst_stalled = False

        if opcode == OP_JMP:
            cond_code = (args >> 5) & 0x7
            target = args & 0x1F
            taken = False

            if cond_code == 0:  # ALWAYS
                taken = True
            elif cond_code == 1:  # !X
                taken = (self.x == 0)
            elif cond_code == 2:  # X-- (Post-decr)
                taken = (self.x != 0)
                self.x = (self.x - 1) & 0xFFFF
            elif cond_code == 3:  # !Y
                taken = (self.y == 0)
            elif cond_code == 4:  # Y-- (Post-decr)
                taken = (self.y != 0)
                self.y = (self.y - 1) & 0xFFFF
            elif cond_code == 5:  # TIMEOUT
                taken = self.timeout_flag
            elif cond_code == 6:  # PIN
                taken = bool((self.gpio_in >> (self.jmp_pin & 7)) & 1)
            elif cond_code == 7:  # !OSRE (OSR not empty)
                taken = (self.osr_count < 16)

            if taken:
                next_pc = target

        elif opcode == OP_WAIT:
            pol = (args >> 7) & 1
            edge = (args >> 6) & 1
            t_en = (args >> 5) & 1
            src = (args >> 3) & 3
            idx = args & 7

            if self._evaluate_wait_condition(pol, edge, src, idx):
                next_pc = default_next_pc
            else:
                inst_stalled = True
                self.stalled = True
                self.stall_reason = "WAIT"
                self.stall_info = {
                    "pol": pol,
                    "edge": edge,
                    "src": src,
                    "idx": idx,
                    "t_en": t_en,
                    "delay": delay,
                    "next_pc": default_next_pc,
                }
                if t_en:
                    self.timeout_counter = self.timeout_val
                    self.timeout_flag = False

        elif opcode == OP_IN:
            src = (args >> 5) & 7
            count_raw = args & 0xF
            count = 16 if count_raw == 0 else count_raw
            mask = (1 << count) - 1

            if src == 0:  # PINS
                val = (self.gpio_in >> (self.in_base & 7)) & mask
            elif src == 1:  # X
                val = self.x & mask
            elif src == 2:  # Y
                val = self.y & mask
            elif src == 3:  # NULL
                val = 0
            elif src == 4:  # ISR
                val = self.isr & mask
            elif src == 5:  # OSR
                val = self.osr & mask
            else:
                val = 0

            if self.shift_in_right:
                self.isr = ((self.isr >> count) | (val << (16 - count))) & 0xFFFF
            else:
                self.isr = ((self.isr << count) | val) & 0xFFFF

            self.isr_count = min(16, self.isr_count + count)

        elif opcode == OP_OUT:
            dest = (args >> 5) & 7
            count_raw = args & 0xF
            count = 16 if count_raw == 0 else count_raw
            mask = (1 << count) - 1

            if dest == 0 and (self.pau_cfg & (1 << 3)) and len(self.pau_tx_queue) > (self.pau_baud if self.pau_baud > 0 else 1) * 2:
                inst_stalled = True
                self.stalled = True
                self.stall_reason = "PAU_TX"
                self.stall_info = {"delay": 0, "next_pc": self.pc}
            else:
                if self.shift_out_right:
                    val = self.osr & mask
                    self.osr = (self.osr >> count) & 0xFFFF
                else:
                    val = (self.osr >> (16 - count)) & mask
                    self.osr = (self.osr << count) & 0xFFFF

                self.osr_count = min(16, self.osr_count + count)

                if dest == 0:  # PINS (via PAU)
                    if self.pau_cfg & (1 << 3):  # Manchester TX Modulator
                        half_period = self.pau_baud if self.pau_baud > 0 else 1
                        bits = []
                        for b in range(count):
                            if self.shift_out_right:
                                bit = (val >> b) & 1
                            else:
                                bit = (val >> (count - 1 - b)) & 1
                            bits.append(bit)

                        pin_seq = []
                        for bit in bits:
                            p1 = 1 if bit == 1 else 0
                            p2 = 0 if bit == 1 else 1
                            pin_seq.extend([p1] * half_period)
                            pin_seq.extend([p2] * half_period)

                        if self.pau_tx_queue:
                            self.pau_tx_queue.extend(pin_seq)
                        else:
                            first_val = pin_seq[0]
                            mask_pin = 1 << (self.out_base & 7)
                            self.gpio_out = ((self.gpio_out & ~mask_pin) | ((first_val << (self.out_base & 7)) & mask_pin)) & 0xFF
                            self._apply_open_drain()
                            self.pau_tx_queue.extend(pin_seq[1:])

                    elif self.pau_cfg & 1:  # NRZI Encode (USB-LS)
                        curr_hw = (self.gpio_out >> (self.out_base & 7)) & 3
                        if curr_hw in (1, 2):
                            self.pau_nrzi_state = curr_hw
                        elif self.pau_nrzi_state not in (1, 2):
                            self.pau_nrzi_state = 1

                        for b in range(count):
                            bit = (val >> b) & 1 if self.shift_out_right else (val >> (count - 1 - b)) & 1
                            if bit == 0:
                                self.pau_nrzi_state = 2 if self.pau_nrzi_state == 1 else 1
                                self.pau_ones_count = 0
                            else:
                                self.pau_ones_count += 1
                                if (self.pau_cfg & 2) and self.pau_ones_count == 6:
                                    self.pau_nrzi_state = 2 if self.pau_nrzi_state == 1 else 1
                                    self.pau_ones_count = 0

                        diff_mask = 0x3 << (self.out_base & 7)
                        self.gpio_out = ((self.gpio_out & ~diff_mask) | ((self.pau_nrzi_state << (self.out_base & 7)) & diff_mask)) & 0xFF
                        self._apply_open_drain()

                    else:
                        pin_mask = (mask << (self.out_base & 7)) & 0xFF
                        self.gpio_out = ((self.gpio_out & ~pin_mask) | ((val << (self.out_base & 7)) & pin_mask)) & 0xFF
                        self._apply_open_drain()
                elif dest == 1:  # X
                    self.x = val & 0xFFFF
                elif dest == 2:  # Y
                    self.y = val & 0xFFFF
                elif dest == 3:  # NULL (discard)
                    pass
                elif dest == 4:  # ISR
                    self.isr = val & 0xFFFF
                    self.isr_count = count
                elif dest == 6:  # PINDIRS
                    pin_mask = (mask << (self.out_base & 7)) & 0xFF
                    self.pindirs = ((self.pindirs & ~pin_mask) | ((val << (self.out_base & 7)) & pin_mask)) & 0xFF
                elif dest == 7:  # PC (Indirect JMP)
                    next_pc = val & 0x1F

        elif opcode == OP_PUSH:
            is_pull = (args >> 7) & 1
            block = (args >> 6) & 1
            ifcond = (args >> 5) & 1

            if is_pull:  # PULL
                if ifcond and self.osr_count < 16:
                    pass  # condition not met, skip
                elif len(self.tx_fifo) > 0:
                    self.osr = self.tx_fifo.popleft() & 0xFFFF
                    self.osr_count = 0
                elif not block:
                    self.osr = self.x & 0xFFFF
                    self.osr_count = 0
                else:
                    inst_stalled = True
                    self.stalled = True
                    self.stall_reason = "PULL"
                    self.stall_info = {"delay": delay, "next_pc": default_next_pc}
            else:  # PUSH
                if ifcond and self.isr_count < 16:
                    pass  # condition not met, skip
                elif len(self.rx_fifo) < self.fifo_depth:
                    self.rx_fifo.append(self.isr & 0xFFFF)
                    self.isr = 0
                    self.isr_count = 0
                elif not block:
                    self.isr = 0
                    self.isr_count = 0
                else:
                    inst_stalled = True
                    self.stalled = True
                    self.stall_reason = "PUSH"
                    self.stall_info = {"delay": delay, "next_pc": default_next_pc}

        elif opcode == OP_ALU:
            dest = (args >> 5) & 7
            op = (args >> 3) & 3
            src = args & 7

            if src == 0:
                src_val = self.gpio_in & 0xFF
            elif src == 1:
                src_val = self.x
            elif src == 2:
                src_val = self.y
            elif src == 3:
                src_val = 0
            elif src == 4:
                src_val = self.isr
            elif src == 5:
                src_val = self.osr
            elif src == 6:
                src_val = 0  # STATUS
            else:
                src_val = 0

            dest_val = {1: self.x, 2: self.y, 4: self.isr, 5: self.osr}.get(dest, 0)

            if op == 0:  # MOV
                res = src_val
            elif op == 1:  # XOR
                res = (dest_val ^ src_val) & 0xFFFF
            elif op == 2:  # ADD
                res = (dest_val + src_val) & 0xFFFF
            elif op == 3:  # REV (16-bit BitRev)
                res = 0
                for b in range(16):
                    if (src_val >> b) & 1:
                        res |= 1 << (15 - b)
            else:
                res = 0

            if dest == 1:
                self.x = res & 0xFFFF
            elif dest == 2:
                self.y = res & 0xFFFF
            elif dest == 4:
                self.isr = res & 0xFFFF
                self.isr_count = 16
            elif dest == 5:
                self.osr = res & 0xFFFF
                self.osr_count = 0

        elif opcode == OP_IRQ:
            clear = (args >> 7) & 1
            wait = (args >> 6) & 1
            peer = (args >> 5) & 1
            idx = args & 7
            flags = self.peer_irq_flags if peer else self.irq_flags

            eff_delay = delay * (self.clkdiv if self.clkdiv > 0 else 1)

            if (self.pau_cfg & (1 << 3)) and len(self.pau_tx_queue) > 0 and not clear:
                inst_stalled = True
                self.stalled = True
                self.stall_reason = "PAU_BUSY"
                self.stall_info = {
                    "peer": peer,
                    "idx": idx,
                    "wait": wait,
                    "delay": eff_delay,
                    "next_pc": default_next_pc,
                }
            else:
                if clear:
                    flags[idx] = False
                else:
                    flags[idx] = True
                    if wait:
                        inst_stalled = True
                        self.stalled = True
                        self.stall_reason = "IRQ"
                        self.stall_info = {
                            "peer": peer,
                            "idx": idx,
                            "delay": eff_delay,
                            "next_pc": default_next_pc,
                        }

        elif opcode == OP_SET:
            dest = (args >> 5) & 7
            val = args & 0x1F

            if dest == 0:  # PINS
                set_mask = ((1 << self.set_count) - 1) & 0x1F
                mask = (set_mask << (self.set_base & 7)) & 0xFF
                self.gpio_out = ((self.gpio_out & ~mask) | ((val << (self.set_base & 7)) & mask)) & 0xFF
                self._apply_open_drain()
                if (self.pau_cfg & 1) and val in (1, 2):
                    self.pau_nrzi_state = val
                    self.pau_ones_count = 0
                if self.pau_cfg & (1 << 3):
                    self.pau_tx_queue.clear()
            elif dest == 1:  # X
                self.x = val & 0xFFFF
            elif dest == 2:  # Y
                self.y = val & 0xFFFF
            elif dest == 3:  # CLKDIV / PRESCALER
                self.clkdiv = val if val > 0 else 1
            elif dest == 4:  # TIMEOUT_VAL
                self.timeout_val = val
                self.timeout_counter = val
                self.timeout_flag = False
            elif dest == 5:  # PAU_BAUD
                self.pau_baud = val
            elif dest == 6:  # PINDIRS
                set_mask = ((1 << self.set_count) - 1) & 0x1F
                mask = (set_mask << (self.set_base & 7)) & 0xFF
                self.pindirs = ((self.pindirs & ~mask) | ((val << (self.set_base & 7)) & mask)) & 0xFF
            elif dest == 7:  # PAU_CFG
                self.pau_cfg = val
                if not (val & (1 << 3)):
                    self.pau_tx_queue.clear()

        # Update control state
        if not inst_stalled:
            eff_delay = delay * (self.clkdiv if self.clkdiv > 0 else 1)
            if eff_delay > 0:
                self.delay_counter = eff_delay
                self._stage_next_pc = next_pc
            else:
                self.pc = next_pc

        self.prev_gpio_in = self.gpio_in & 0xFF
        row = self._format_trace_row(trace_pc, self._current_instr_str)
        self.history.append(row)

    def _format_trace_row(self, pc: int, instr_str: str) -> str:
        """Format the cycle's execution state into a fixed-width row."""
        stall_str = "1" if self.stalled else "0"
        return (
            f"{self.cycle_count:4d} | "
            f"{pc:2d} | "
            f"{instr_str:<18} | "
            f"{self.delay_counter:3d} | "
            f"0x{self.x:04x} | "
            f"0x{self.y:04x} | "
            f"0x{self.osr:04x} | "
            f"0x{self.isr:04x} | "
            f"  0x{self.gpio_in:02x} | "
            f"   0x{self.gpio_out:02x} | "
            f"{stall_str:^5}"
        )

    def get_trace_row(self, cycle: int | None = None) -> str:
        """Return the most recently formatted trace row (or current state)."""
        if self.history:
            return self.history[-1]
        word = self.memory[self.pc] & 0xFFFF
        instr_str = disassemble(word, self.side_set_count, self.side_set_opt)
        return self._format_trace_row(self.pc, instr_str)

    def get_trace_table(self, last_n: int = 15) -> str:
        """Return a compact rolling window trace table (e.g., last 15 cycles)."""
        lines = [HEADER, SEPARATOR]
        rows = list(self.history)[-last_n:]
        lines.extend(rows)
        return "\n".join(lines)

    def run(self, max_cycles: int = 1000) -> int:
        """Run execution until stalled or max_cycles reached. Returns cycles run."""
        for _ in range(max_cycles):
            self.step()
            if self.stalled:
                break
        return self.cycle_count


def main() -> None:
    """CLI entry point for emu.py."""
    parser = argparse.ArgumentParser(description="openPIO Minimal Emulator")
    parser.add_argument("input", help="Binary (.bin) or Assembly (.asm) file")
    parser.add_argument("-c", "--cycles", type=int, default=32, help="Number of cycles to run")
    args = parser.parse_args()

    if args.input.endswith(".asm"):
        with open(args.input, "r", encoding="utf-8") as f:
            prog = asm.assemble(f.read())
    else:
        with open(args.input, "rb") as f:
            data = f.read()
            count = len(data) // 2
            prog = list(struct.unpack(f"<{count}H", data[:count * 2]))

    sm = StateMachine(prog)
    for _ in range(args.cycles):
        sm.step()

    print(sm.get_trace_table(args.cycles))


if __name__ == "__main__":
    main()
