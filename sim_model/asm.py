"""asm.py - Minimal, robust Assembler for 16-bit Custom PIO ASIC Architecture.

Based on ISA.md specification.
Converts human-readable assembly text into raw 16-bit binary instruction words.
"""

from __future__ import annotations

import argparse
import re
import struct
import sys


class AssemblerError(Exception):
    """Raised when an error occurs during assembly."""

    def __init__(self, message: str, line_num: int | None = None, line_text: str | None = None) -> None:
        self.message = message
        self.line_num = line_num
        self.line_text = line_text
        prefix = f"Line {line_num}: " if line_num is not None else ""
        context = f"\n  --> {line_text}" if line_text is not None else ""
        super().__init__(f"{prefix}{message}{context}")


# ISA Opcodes (Bits [15:13])
OP_JMP = 0
OP_WAIT = 1
OP_IN = 2
OP_OUT = 3
OP_PUSH = 4
OP_ALU = 5
OP_IRQ = 6
OP_SET = 7

# JMP Conditions (Bits [7:5])
JMP_COND_MAP: dict[str, int] = {
    "": 0,
    "always": 0,
    "!x": 1,
    "not_x": 1,
    "x_zero": 1,
    "x--": 2,
    "x-": 2,
    "!y": 3,
    "not_y": 3,
    "y_zero": 3,
    "y--": 4,
    "y-": 4,
    "timeout": 5,
    "pin": 6,
    "!osre": 7,
    "not_osre": 7,
}

REV_JMP_COND_MAP: dict[int, str] = {
    0: "",
    1: "!x",
    2: "x--",
    3: "!y",
    4: "y--",
    5: "timeout",
    6: "pin",
    7: "!osre",
}

# WAIT Sources (Bits [4:3])
WAIT_SRC_MAP: dict[str, int] = {
    "gpio": 0,
    "pin": 0,
    "in_base": 1,
    "base": 1,
    "irq": 2,
    "peer_irq": 3,
    "sm_peer_irq": 3,
    "peer": 3,
}

REV_WAIT_SRC_MAP: dict[int, str] = {
    0: "gpio",
    1: "in_base",
    2: "irq",
    3: "peer_irq",
}

# IN Sources (Bits [7:5])
IN_SRC_MAP: dict[str, int] = {
    "pins": 0,
    "x": 1,
    "y": 2,
    "null": 3,
    "isr": 4,
    "osr": 5,
}

REV_IN_SRC_MAP: dict[int, str] = {
    0: "pins",
    1: "x",
    2: "y",
    3: "null",
    4: "isr",
    5: "osr",
}

# OUT Destinations (Bits [7:5])
OUT_DEST_MAP: dict[str, int] = {
    "pins": 0,
    "x": 1,
    "y": 2,
    "null": 3,
    "isr": 4,
    "reserved": 5,
    "pindirs": 6,
    "pc": 7,
}

REV_OUT_DEST_MAP: dict[int, str] = {
    0: "pins",
    1: "x",
    2: "y",
    3: "null",
    4: "isr",
    5: "reserved",
    6: "pindirs",
    7: "pc",
}

# ALU Operations (Bits [4:3])
ALU_OP_MAP: dict[str, int] = {
    "mov": 0,
    "xor": 1,
    "add": 2,
    "rev": 3,
}

REV_ALU_OP_MAP: dict[int, str] = {
    0: "mov",
    1: "xor",
    2: "add",
    3: "rev",
}

# ALU Destinations (Bits [7:5])
ALU_DEST_MAP: dict[str, int] = {
    "x": 1,
    "y": 2,
    "isr": 4,
    "osr": 5,
}

REV_ALU_DEST_MAP: dict[int, str] = {
    1: "x",
    2: "y",
    4: "isr",
    5: "osr",
}

# ALU Sources (Bits [2:0])
ALU_SRC_MAP: dict[str, int] = {
    "pins": 0,
    "x": 1,
    "y": 2,
    "null": 3,
    "isr": 4,
    "osr": 5,
    "status": 6,
}

REV_ALU_SRC_MAP: dict[int, str] = {
    0: "pins",
    1: "x",
    2: "y",
    3: "null",
    4: "isr",
    5: "osr",
    6: "status",
}

# SET Destinations (Bits [7:5])
SET_DEST_MAP: dict[str, int] = {
    "pins": 0,
    "x": 1,
    "y": 2,
    "clkdiv": 3,
    "prescaler": 3,
    "timeout_val": 4,
    "timeout": 4,
    "pau_baud": 5,
    "baud": 5,
    "pindirs": 6,
    "pau_cfg": 7,
    "pau": 7,
}

REV_SET_DEST_MAP: dict[int, str] = {
    0: "pins",
    1: "x",
    2: "y",
    3: "clkdiv",
    4: "timeout_val",
    5: "pau_baud",
    6: "pindirs",
    7: "pau_cfg",
}

PAU_CFG_FLAGS: dict[str, int] = {
    "nrzi": 1 << 0,
    "stuff": 1 << 1,
    "bit_stuff": 1 << 1,
    "parity": 1 << 2,
    "manchester_tx": 1 << 3,
    "manchester_rx": 1 << 4,
}


class Program(list[int]):
    """Assembled PIO program container inheriting from list[int].

    Behaves as a list of 16-bit integers with metadata attributes.
    """

    def __init__(
        self,
        words: list[int],
        name: str = "program",
        wrap_bottom: int = 0,
        wrap_top: int | None = None,
        side_set_count: int = 0,
        side_set_opt: bool = False,
        side_set_pindirs: bool = False,
        open_drain_mask: int = 0,
        clkdiv_int: int = 1,
        clkdiv_frac: int = 0,
        has_explicit_open_drain: bool = False,
        has_explicit_clkdiv: bool = False,
    ) -> None:
        super().__init__(words)
        self.name = name
        self.wrap_bottom = wrap_bottom
        self.wrap_top = wrap_top if wrap_top is not None else max(0, len(words) - 1)
        self.wrap_target = self.wrap_bottom
        self.wrap = self.wrap_top
        self.side_set_count = side_set_count
        self.side_set_opt = side_set_opt
        self.side_set_pindirs = side_set_pindirs
        self.open_drain_mask = open_drain_mask & 0xFF
        self.clkdiv_int = clkdiv_int
        self.clkdiv_frac = clkdiv_frac & 0xFF
        self._has_explicit_open_drain = has_explicit_open_drain
        self._has_explicit_clkdiv = has_explicit_clkdiv

    def to_bytes(self) -> bytes:
        """Return raw little-endian 16-bit words."""
        return struct.pack(f"<{len(self)}H", *self)


def _parse_int(token: str, line_num: int, line_text: str) -> int:
    try:
        return int(token, 0)
    except ValueError:
        raise AssemblerError(f"Invalid integer literal '{token}'", line_num, line_text)


def _encode_delay_sideset(
    delay: int,
    side: int | None,
    side_set_count: int,
    side_set_opt: bool,
    line_num: int,
    line_text: str,
) -> int:
    total_ss_bits = side_set_count + (1 if side_set_opt else 0)
    delay_bits = 5 - total_ss_bits
    if delay_bits < 0:
        raise AssemblerError(
            f"Invalid side-set configuration: {total_ss_bits} bits exceeds 5-bit field",
            line_num,
            line_text,
        )

    max_delay = (1 << delay_bits) - 1
    if not (0 <= delay <= max_delay):
        raise AssemblerError(
            f"Delay ({delay}) exceeds maximum allowed ({max_delay}) with {side_set_count} side-set bits",
            line_num,
            line_text,
        )

    val = delay & max_delay
    if side_set_count > 0:
        max_side = (1 << side_set_count) - 1
        if side_set_opt:
            if side is not None:
                if not (0 <= side <= max_side):
                    raise AssemblerError(
                        f"Side-set value ({side}) out of range (0-{max_side})",
                        line_num,
                        line_text,
                    )
                ss_field = (1 << side_set_count) | (side & max_side)
                val |= (ss_field << delay_bits)
        else:
            if side is None:
                side = 0
            if not (0 <= side <= max_side):
                raise AssemblerError(
                    f"Side-set value ({side}) out of range (0-{max_side})",
                    line_num,
                    line_text,
                )
            val |= ((side & max_side) << delay_bits)
    elif side is not None:
        raise AssemblerError(
            "Side-set value specified but .side_set is 0",
            line_num,
            line_text,
        )

    return val & 0x1F


def disassemble(word: int, side_set_count: int = 0, side_set_opt: bool = False) -> str:
    """Disassemble a 16-bit PIO instruction word into human-readable text."""
    word &= 0xFFFF
    opcode = (word >> 13) & 0x7
    delay_ss = (word >> 8) & 0x1F
    args = word & 0xFF

    # Decode delay and side-set
    total_ss_bits = side_set_count + (1 if side_set_opt else 0)
    delay_bits = 5 - total_ss_bits
    delay_mask = (1 << delay_bits) - 1
    delay = delay_ss & delay_mask
    side = None

    if side_set_count > 0:
        ss_field = delay_ss >> delay_bits
        if side_set_opt:
            en_bit = (ss_field >> side_set_count) & 1
            if en_bit:
                side = ss_field & ((1 << side_set_count) - 1)
        else:
            side = ss_field & ((1 << side_set_count) - 1)

    side_str = f" side {side}" if side is not None else ""
    delay_str = f" [{delay}]" if delay > 0 else ""
    suffix = f"{side_str}{delay_str}"

    if opcode == OP_JMP:
        cond_code = (args >> 5) & 0x7
        target = args & 0x1F
        cond_str = REV_JMP_COND_MAP.get(cond_code, "")
        if cond_str:
            return f"jmp {cond_str}, {target}{suffix}"
        return f"jmp {target}{suffix}"

    elif opcode == OP_WAIT:
        pol = (args >> 7) & 1
        edge = (args >> 6) & 1
        t_en = (args >> 5) & 1
        src = (args >> 3) & 3
        idx = args & 7
        src_str = REV_WAIT_SRC_MAP.get(src, "gpio")
        edge_str = " edge" if edge else ""
        tout_str = " timeout" if t_en else ""
        return f"wait {pol} {src_str} {idx}{edge_str}{tout_str}{suffix}"

    elif opcode == OP_IN:
        src = (args >> 5) & 7
        count_raw = args & 0xF
        count = 16 if count_raw == 0 else count_raw
        src_str = REV_IN_SRC_MAP.get(src, "pins")
        return f"in {src_str}, {count}{suffix}"

    elif opcode == OP_OUT:
        dest = (args >> 5) & 7
        count_raw = args & 0xF
        count = 16 if count_raw == 0 else count_raw
        dest_str = REV_OUT_DEST_MAP.get(dest, "pins")
        return f"out {dest_str}, {count}{suffix}"

    elif opcode == OP_PUSH:
        is_pull = (args >> 7) & 1
        block = (args >> 6) & 1
        ifcond = (args >> 5) & 1
        mnemonic = "pull" if is_pull else "push"
        parts = [mnemonic]
        if not block:
            parts.append("noblock")
        if ifcond:
            parts.append("ifempty" if is_pull else "iffull")
        return f"{' '.join(parts)}{suffix}"

    elif opcode == OP_ALU:
        dest = (args >> 5) & 7
        op = (args >> 3) & 3
        src = args & 7
        dest_str = REV_ALU_DEST_MAP.get(dest, f"dest_{dest}")
        op_str = REV_ALU_OP_MAP.get(op, "mov")
        src_str = REV_ALU_SRC_MAP.get(src, f"src_{src}")
        if op_str == "mov" and dest_str == "y" and src_str == "y":
            return f"nop{suffix}"
        return f"{op_str} {dest_str}, {src_str}{suffix}"

    elif opcode == OP_IRQ:
        clear = (args >> 7) & 1
        wait = (args >> 6) & 1
        peer = (args >> 5) & 1
        idx = args & 7
        parts = ["irq"]
        if clear:
            parts.append("clear")
        if wait:
            parts.append("wait")
        if peer:
            parts.append("peer")
        parts.append(str(idx))
        return f"{' '.join(parts)}{suffix}"

    elif opcode == OP_SET:
        dest = (args >> 5) & 7
        val = args & 0x1F
        dest_str = REV_SET_DEST_MAP.get(dest, f"dest_{dest}")
        return f"set {dest_str}, {val}{suffix}"

    return f".word 0x{word:04x}"


def assemble(source_code: str) -> list[int]:
    """Assemble source code text into a list of 16-bit integer words (max 32)."""
    lines = source_code.splitlines()

    prog_name = "program"
    side_set_count = 0
    side_set_opt = False
    side_set_pindirs = False
    open_drain_mask = 0
    clkdiv_int = 1
    clkdiv_frac = 0
    has_explicit_open_drain = False
    has_explicit_clkdiv = False
    wrap_bottom: int | None = None
    wrap_top: int | None = None

    labels: dict[str, int] = {}
    instructions: list[tuple[int, str, int]] = []  # (addr, text, line_num)

    current_addr = 0

    # Pass 1: Parse directives, labels, and collect instructions
    for line_idx, raw_line in enumerate(lines):
        line_num = line_idx + 1

        # Strip comments (; or // or #)
        code_part = re.split(r";|//|#", raw_line, maxsplit=1)[0].strip()
        if not code_part:
            continue

        # Check for directive
        if code_part.startswith("."):
            parts = code_part.split()
            directive = parts[0].lower()
            if directive == ".program":
                if len(parts) > 1:
                    prog_name = parts[1]
            elif directive == ".side_set":
                if len(parts) < 2:
                    raise AssemblerError(".side_set requires bit count argument", line_num, raw_line)
                side_set_count = _parse_int(parts[1], line_num, raw_line)
                if not (0 <= side_set_count <= 5):
                    raise AssemblerError("Side-set bit count must be between 0 and 5", line_num, raw_line)
                side_set_opt = any(p.lower() in ("opt", "optional") for p in parts[2:])
                side_set_pindirs = any(p.lower() in ("pindirs", "pindir") for p in parts[2:])
            elif directive in (".open_drain", ".opendrain"):
                if len(parts) < 2:
                    raise AssemblerError(".open_drain requires mask or pin list", line_num, raw_line)
                has_explicit_open_drain = True
                arg_str = "".join(parts[1:])
                if "," in arg_str:
                    mask = 0
                    for p in arg_str.split(","):
                        if p.strip():
                            mask |= (1 << _parse_int(p.strip(), line_num, raw_line))
                    open_drain_mask = mask & 0xFF
                else:
                    open_drain_mask = _parse_int(arg_str, line_num, raw_line) & 0xFF
            elif directive in (".clkdiv", ".prescaler"):
                if len(parts) < 2:
                    raise AssemblerError(f"{directive} requires divider argument", line_num, raw_line)
                has_explicit_clkdiv = True
                clkdiv_int = _parse_int(parts[1], line_num, raw_line)
                if len(parts) > 2:
                    clkdiv_frac = _parse_int(parts[2], line_num, raw_line) & 0xFF
            elif directive == ".wrap_target":
                wrap_bottom = current_addr
            elif directive == ".wrap":
                wrap_top = max(0, current_addr - 1)
            elif directive == ".word":
                if len(parts) < 2:
                    raise AssemblerError(".word requires a value argument", line_num, raw_line)
                instructions.append((current_addr, code_part, line_num))
                current_addr += 1
            else:
                raise AssemblerError(f"Unknown directive '{directive}'", line_num, raw_line)
            continue

        # Check for label(s)
        while True:
            label_match = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*(.*)$", code_part)
            if not label_match:
                break
            label = label_match.group(1).lower()
            if label in labels:
                raise AssemblerError(f"Duplicate label '{label}'", line_num, raw_line)
            labels[label] = current_addr
            code_part = label_match.group(2).strip()

        if not code_part:
            continue

        if current_addr >= 32:
            raise AssemblerError(
                f"Program exceeds maximum 32 instructions (at instruction {current_addr + 1})",
                line_num,
                raw_line,
            )

        instructions.append((current_addr, code_part, line_num))
        current_addr += 1

    if current_addr > 32:
        raise AssemblerError(f"Program exceeds maximum 32 instructions ({current_addr} instructions)")

    if wrap_bottom is None:
        wrap_bottom = 0
    if wrap_top is None:
        wrap_top = max(0, current_addr - 1)

    # Pass 2: Encode instructions
    words: list[int] = []

    for addr, text, line_num in instructions:
        # Handle raw .word directive
        if text.startswith(".word"):
            val_str = text.split()[1]
            words.append(_parse_int(val_str, line_num, text) & 0xFFFF)
            continue

        # Extract [delay]
        delay = 0
        delay_match = re.search(r"\[\s*([0-9a-zA-Z_]+)\s*\]", text)
        if delay_match:
            delay = _parse_int(delay_match.group(1), line_num, text)
            text = text[:delay_match.start()] + text[delay_match.end():]

        # Extract side <val>
        side = None
        side_match = re.search(r"\bside\s+([0-9a-zA-Z_]+)\b", text, re.IGNORECASE)
        if side_match:
            side = _parse_int(side_match.group(1), line_num, text)
            text = text[:side_match.start()] + text[side_match.end():]

        delay_ss = _encode_delay_sideset(delay, side, side_set_count, side_set_opt, line_num, text)

        # Tokenize instruction arguments
        clean_text = text.strip()
        tokens = [t.strip() for t in re.split(r"[\s,]+", clean_text) if t.strip()]
        if not tokens:
            raise AssemblerError("Empty instruction line", line_num, text)

        mnemonic = tokens[0].lower()
        args = tokens[1:]

        # Alias: nop -> mov y, y
        if mnemonic == "nop":
            mnemonic = "mov"
            args = ["y", "y"]

        if mnemonic == "jmp":
            if len(args) == 1:
                cond_code = 0  # ALWAYS
                target_str = args[0]
            elif len(args) == 2:
                cond_str = args[0].lower()
                if cond_str not in JMP_COND_MAP:
                    raise AssemblerError(
                        f"Invalid JMP condition '{cond_str}'. Valid: {list(JMP_COND_MAP.keys())}",
                        line_num,
                        clean_text,
                    )
                cond_code = JMP_COND_MAP[cond_str]
                target_str = args[1]
            else:
                raise AssemblerError("JMP expects: jmp [cond,] target", line_num, clean_text)

            target_lower = target_str.lower()
            if target_lower in labels:
                target = labels[target_lower]
            else:
                target = _parse_int(target_str, line_num, clean_text)

            if not (0 <= target <= 31):
                raise AssemblerError(f"JMP target address out of range (0-31): {target}", line_num, clean_text)

            args_byte = (cond_code << 5) | (target & 0x1F)
            words.append((OP_JMP << 13) | (delay_ss << 8) | args_byte)

        elif mnemonic == "wait":
            if len(args) < 3:
                raise AssemblerError("WAIT expects: wait <polarity> <source> <index>", line_num, clean_text)

            pol_str = args[0].lower()
            edge = 0
            if pol_str in ("1", "high", "hi"):
                polarity = 1
            elif pol_str in ("0", "low", "lo"):
                polarity = 0
            elif pol_str in ("rise", "rising"):
                polarity = 1
                edge = 1
            elif pol_str in ("fall", "falling"):
                polarity = 0
                edge = 1
            else:
                raise AssemblerError(f"Invalid WAIT polarity: '{pol_str}'", line_num, clean_text)

            src_str = args[1].lower()
            if src_str not in WAIT_SRC_MAP:
                raise AssemblerError(f"Invalid WAIT source: '{src_str}'", line_num, clean_text)
            src_code = WAIT_SRC_MAP[src_str]

            index = _parse_int(args[2], line_num, clean_text)
            if not (0 <= index <= 7):
                raise AssemblerError(f"WAIT pin/IRQ index out of range (0-7): {index}", line_num, clean_text)

            t_en = 0
            for extra in args[3:]:
                extra_lower = extra.lower()
                if extra_lower in ("timeout", "t_en", "tout"):
                    t_en = 1
                elif extra_lower == "edge":
                    edge = 1
                elif extra_lower == "level":
                    edge = 0
                else:
                    raise AssemblerError(f"Invalid WAIT argument: '{extra}'", line_num, clean_text)

            args_byte = (polarity << 7) | (edge << 6) | (t_en << 5) | (src_code << 3) | (index & 7)
            words.append((OP_WAIT << 13) | (delay_ss << 8) | args_byte)

        elif mnemonic == "in":
            if len(args) != 2:
                raise AssemblerError("IN requires: in <source>, <count>", line_num, clean_text)
            src_str = args[0].lower()
            if src_str not in IN_SRC_MAP:
                raise AssemblerError(f"Invalid IN source: '{src_str}'", line_num, clean_text)
            src_code = IN_SRC_MAP[src_str]

            bit_count = _parse_int(args[1], line_num, clean_text)
            if not (1 <= bit_count <= 16):
                raise AssemblerError(f"IN bit count out of range (1-16): {bit_count}", line_num, clean_text)
            count_enc = 0 if bit_count == 16 else bit_count

            args_byte = (src_code << 5) | (count_enc & 0xF)
            words.append((OP_IN << 13) | (delay_ss << 8) | args_byte)

        elif mnemonic == "out":
            if len(args) != 2:
                raise AssemblerError("OUT requires: out <dest>, <count>", line_num, clean_text)
            dest_str = args[0].lower()
            if dest_str not in OUT_DEST_MAP:
                raise AssemblerError(f"Invalid OUT destination: '{dest_str}'", line_num, clean_text)
            dest_code = OUT_DEST_MAP[dest_str]

            bit_count = _parse_int(args[1], line_num, clean_text)
            if not (1 <= bit_count <= 16):
                raise AssemblerError(f"OUT bit count out of range (1-16): {bit_count}", line_num, clean_text)
            count_enc = 0 if bit_count == 16 else bit_count

            args_byte = (dest_code << 5) | (count_enc & 0xF)
            words.append((OP_OUT << 13) | (delay_ss << 8) | args_byte)

        elif mnemonic in ("push", "pull"):
            is_pull = 1 if mnemonic == "pull" else 0
            block_bit = 1  # default blocking
            ifcond_bit = 0

            for a in args:
                a_lower = a.lower()
                if a_lower in ("block", "blocking"):
                    block_bit = 1
                elif a_lower in ("noblock", "nonblock", "nonblocking"):
                    block_bit = 0
                elif a_lower in ("iffull", "if_full") and not is_pull:
                    ifcond_bit = 1
                elif a_lower in ("ifempty", "if_empty") and is_pull:
                    ifcond_bit = 1
                elif a_lower in ("ifcond", "cond"):
                    ifcond_bit = 1
                else:
                    raise AssemblerError(f"Invalid argument for {mnemonic}: '{a}'", line_num, clean_text)

            args_byte = (is_pull << 7) | (block_bit << 6) | (ifcond_bit << 5)
            words.append((OP_PUSH << 13) | (delay_ss << 8) | args_byte)

        elif mnemonic in ("mov", "xor", "add", "rev", "alu"):
            if mnemonic == "alu":
                if len(args) != 3:
                    raise AssemblerError("ALU syntax: alu <dest>, <op>, <src>", line_num, clean_text)
                dest_str = args[0].lower()
                op_str = args[1].lower()
                src_str = args[2].lower()
            else:
                if len(args) != 2:
                    raise AssemblerError(f"{mnemonic} syntax: {mnemonic} <dest>, <src>", line_num, clean_text)
                dest_str = args[0].lower()
                op_str = mnemonic
                src_str = args[1].lower()

            if dest_str not in ALU_DEST_MAP:
                raise AssemblerError(f"Invalid ALU destination: '{dest_str}'", line_num, clean_text)
            if op_str not in ALU_OP_MAP:
                raise AssemblerError(f"Invalid ALU operation: '{op_str}'", line_num, clean_text)
            if src_str not in ALU_SRC_MAP:
                raise AssemblerError(f"Invalid ALU source: '{src_str}'", line_num, clean_text)

            dest_code = ALU_DEST_MAP[dest_str]
            op_code = ALU_OP_MAP[op_str]
            src_code = ALU_SRC_MAP[src_str]

            args_byte = (dest_code << 5) | (op_code << 3) | src_code
            words.append((OP_ALU << 13) | (delay_ss << 8) | args_byte)

        elif mnemonic == "irq":
            clear_bit = 0
            wait_bit = 0
            peer_bit = 0
            idx: int | None = None

            for a in args:
                a_lower = a.lower()
                if a_lower == "set":
                    clear_bit = 0
                elif a_lower in ("clear", "clr"):
                    clear_bit = 1
                elif a_lower in ("wait", "stall"):
                    wait_bit = 1
                elif a_lower in ("cont", "nowait"):
                    wait_bit = 0
                elif a_lower in ("peer", "peersm", "peer_sm"):
                    peer_bit = 1
                elif a_lower == "local":
                    peer_bit = 0
                else:
                    val = _parse_int(a, line_num, clean_text)
                    if idx is not None:
                        raise AssemblerError(f"Unexpected extra argument in IRQ: '{a}'", line_num, clean_text)
                    idx = val

            if idx is None:
                raise AssemblerError("IRQ instruction requires an IRQ index (0-7)", line_num, clean_text)
            if not (0 <= idx <= 7):
                raise AssemblerError(f"IRQ index out of range (0-7): {idx}", line_num, clean_text)

            args_byte = (clear_bit << 7) | (wait_bit << 6) | (peer_bit << 5) | (idx & 7)
            words.append((OP_IRQ << 13) | (delay_ss << 8) | args_byte)

        elif mnemonic in ("set", "cfg"):
            if len(args) < 2:
                raise AssemblerError(f"{mnemonic} requires: {mnemonic} <dest>, <val>", line_num, clean_text)
            dest_str = args[0].lower()
            if dest_str not in SET_DEST_MAP:
                raise AssemblerError(f"Invalid SET destination: '{dest_str}'", line_num, clean_text)
            dest_code = SET_DEST_MAP[dest_str]

            val_str = "".join(args[1:]).lower()
            if dest_code == 7 and ("|" in val_str or any(f in val_str for f in PAU_CFG_FLAGS)):
                cfg_val = 0
                for part in val_str.split("|"):
                    p = part.strip()
                    if p in PAU_CFG_FLAGS:
                        cfg_val |= PAU_CFG_FLAGS[p]
                    else:
                        cfg_val |= _parse_int(p, line_num, clean_text)
                val = cfg_val
            else:
                val = _parse_int(val_str, line_num, clean_text)

            if not (0 <= val <= 31):
                raise AssemblerError(f"SET value out of range (0-31): {val}", line_num, clean_text)

            args_byte = (dest_code << 5) | (val & 0x1F)
            words.append((OP_SET << 13) | (delay_ss << 8) | args_byte)

        else:
            raise AssemblerError(f"Unknown instruction mnemonic: '{mnemonic}'", line_num, clean_text)

    return Program(
        words,
        name=prog_name,
        wrap_bottom=wrap_bottom,
        wrap_top=wrap_top,
        side_set_count=side_set_count,
        side_set_opt=side_set_opt,
        side_set_pindirs=side_set_pindirs,
        open_drain_mask=open_drain_mask,
        clkdiv_int=clkdiv_int,
        clkdiv_frac=clkdiv_frac,
        has_explicit_open_drain=has_explicit_open_drain,
        has_explicit_clkdiv=has_explicit_clkdiv,
    )


def main() -> None:
    """CLI entry point for asm.py."""
    parser = argparse.ArgumentParser(description="openPIO Minimal Assembler")
    parser.add_argument("input", help="Source assembly file (.asm)")
    parser.add_argument("-o", "--output", help="Output binary file (.bin)")
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        src = f.read()

    try:
        words = assemble(src)
    except AssemblerError as e:
        sys.stderr.write(f"Assembly Error: {e}\n")
        sys.exit(1)

    out_path = args.output
    if not out_path:
        out_path = args.input.rsplit(".", 1)[0] + ".bin"

    with open(out_path, "wb") as f:
        f.write(struct.pack(f"<{len(words)}H", *words))

    print(f"Assembled {len(words)} instructions to {out_path}")


if __name__ == "__main__":
    main()
