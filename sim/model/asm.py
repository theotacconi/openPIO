"""asm.py - Assembler for the openPIO ISA v2 (ISA.md).

Converts assembly text into 16-bit instruction words (max 64), plus the per-SM configuration
the program needs (wrap, side-set, conditions, PAU, CRC, thresholds) as Program attributes.
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


IMEM_WORDS = 64

# Opcodes (bits [15:13]); JMP and ALU take two (bit 13 = target bit 5 / operation bit 2)
OP_JMP = 0
OP_IN = 2
OP_OUT = 3
OP_ALU = 4
OP_CTRL = 6
OP_SET = 7

# Control (opcode 110) types, bits [7:6]
CT_IRQ, CT_FIFO, CT_WAIT, CT_PAU = 0, 1, 2, 3

# JMP conditions (bits [7:5])
JC_ALWAYS, JC_NEVER, JC_XDEC, JC_YDEC = 0, 1, 2, 3   # 4..7 = COND0..COND3

# Operand codes (IN source, OUT destination, ALU source / destination)
R_PINS, R_PINDIRS, R_X, R_Y, R_ISR, R_OSR, R_NULL, R_SPECIAL = range(8)
OPERAND_NAMES = ["pins", "pindirs", "x", "y", "isr", "osr", "null", "special"]
IN_SRC = {"pins": R_PINS, "x": R_X, "y": R_Y, "isr": R_ISR, "osr": R_OSR, "null": R_NULL,
          "status": R_SPECIAL}
OUT_DEST = {"pins": R_PINS, "pindirs": R_PINDIRS, "x": R_X, "y": R_Y, "isr": R_ISR,
            "null": R_NULL, "pc": R_SPECIAL}
ALU_DEST = {"pins": R_PINS, "pindirs": R_PINDIRS, "x": R_X, "y": R_Y, "isr": R_ISR, "osr": R_OSR,
            "null": R_NULL, "cond": R_SPECIAL}
ALU_SRC = {**ALU_DEST, "status": R_SPECIAL}
del ALU_SRC["cond"]

ALU_OPS = ["mov", "rev", "and", "or", "xor", "xnor", "add", "sub"]

SET_DEST = {"pins": 0, "pindirs": 1, "x": 2, "y": 3, "dlymul": 4,
            "timeout_val": 5, "timeout": 5, "pau_baud": 6, "baud": 6, "pau_ctl": 7}
REV_SET_DEST = ["pins", "pindirs", "x", "y", "dlymul", "timeout_val", "pau_baud", "pau_ctl"]
PAU_CTL_FLAGS = {"tx": 1, "rx": 2, "stuff": 4, "crc": 8, "monitor": 16}

PAU_ACTIONS = {"crc_reset": 0, "crc_append": 1, "tx_abort": 2, "crc_feed": 3}

# Condition sources (bits [6:4] of a condition) and named status indices
CS_GPIO, CS_INBASE, CS_IRQ, CS_PEER, CS_XBIT, CS_YBIT, CS_STATUS, CS_PAU = range(8)
COND_SRC = {"gpio": CS_GPIO, "pin": CS_GPIO, "in_base": CS_INBASE, "base": CS_INBASE,
            "irq": CS_IRQ, "peer_irq": CS_PEER, "peer": CS_PEER, "sm_peer_irq": CS_PEER,
            "x": CS_XBIT, "y": CS_YBIT, "status": CS_STATUS, "pau": CS_PAU}
REV_COND_SRC = ["gpio", "in_base", "irq", "peer_irq", "x", "y", "status", "pau"]
STATUS_NAMES = {"x_zero": 0, "y_zero": 1, "x_eq_y": 2, "timeout": 3,
                "osr_empty": 4, "isr_full": 5, "tx_empty": 6, "rx_full": 7}
PAU_STATUS_NAMES = {"tx_idle": 0, "rx_idle": 1, "tx_ready": 2, "rx_ready": 3,
                    "crc_ok": 4, "crc_zero": 5, "stuff_err": 6, "overrun": 7, "se0": 8, "se1": 9,
                    "bit_err": 10}

# ISA v1 condition names: (inverted, source, index)
NAMED_CONDS = {
    "!x": (0, CS_STATUS, 0), "not_x": (0, CS_STATUS, 0), "x_zero": (0, CS_STATUS, 0),
    "!y": (0, CS_STATUS, 1), "not_y": (0, CS_STATUS, 1), "y_zero": (0, CS_STATUS, 1),
    "x==y": (0, CS_STATUS, 2), "x!=y": (1, CS_STATUS, 2),
    "timeout": (0, CS_STATUS, 3),
    "!osre": (1, CS_STATUS, 4), "not_osre": (1, CS_STATUS, 4), "osre": (0, CS_STATUS, 4),
}

# Chip reset values of COND_CFG (ISA.md, Conditions)
COND_RESET = [0x60, 0x61, 0x00, 0xE4]

# PAU_MODE fields
PAU_ENC = {"nrz": 0, "nrzi": 1, "manchester": 2, "bmc": 3, "biphase_mark": 3}
STUFF_MODES = {"ones": 0, "zeros": 1, "equal": 2}


def cond_byte(inv: int, src: int, idx: int) -> int:
    return ((inv & 1) << 7) | ((src & 7) << 4) | (idx & 0xF)


def cond_text(c: int) -> str:
    src, idx = (c >> 4) & 7, c & 0xF
    names = STATUS_NAMES if src == CS_STATUS else PAU_STATUS_NAMES if src == CS_PAU else {}
    idx_str = next((n for n, v in names.items() if v == idx), str(idx))
    return f"{'!' if c & 0x80 else ''}{REV_COND_SRC[src]} {idx_str}"


class Program(list[int]):
    """Assembled program: a list of 16-bit words with the configuration it needs.

    cond_cfg: COND_CFG (4 condition bytes); pin_slot: slot used by `pin` (the v1 jmp_pin, whose
    index a loader may override); pau_mode, crc_cfg, crc_poly, crc_res: PAU registers.
    """

    def __init__(self, words: list[int], name: str = "program", **cfg) -> None:
        super().__init__(words)
        self.name = name
        self.wrap_bottom = cfg.get("wrap_bottom", 0)
        self.wrap_top = cfg.get("wrap_top", max(0, len(words) - 1))
        self.wrap_target = self.wrap_bottom
        self.wrap = self.wrap_top
        self.side_set_count = cfg.get("side_set_count", 0)
        self.side_set_opt = cfg.get("side_set_opt", False)
        self.side_set_pindirs = cfg.get("side_set_pindirs", False)
        self.open_drain_mask = cfg.get("open_drain_mask", 0) & 0xFF
        self.clkdiv_int = cfg.get("clkdiv_int", 1)
        self.clkdiv_frac = cfg.get("clkdiv_frac", 0) & 0xFF
        self._has_explicit_open_drain = cfg.get("has_explicit_open_drain", False)
        self._has_explicit_clkdiv = cfg.get("has_explicit_clkdiv", False)
        self.cond_cfg = list(cfg.get("cond_cfg", COND_RESET))
        self.pin_slot = cfg.get("pin_slot")
        self.push_thresh = cfg.get("push_thresh", 0)
        self.pull_thresh = cfg.get("pull_thresh", 0)
        self.pau_mode = cfg.get("pau_mode", 0)
        self.crc_cfg = cfg.get("crc_cfg", 0)
        self.crc_poly = cfg.get("crc_poly", 0)
        self.crc_res = cfg.get("crc_res", 0)

    def to_bytes(self) -> bytes:
        """Return raw little-endian 16-bit words."""
        return struct.pack(f"<{len(self)}H", *self)


def _parse_int(token: str, line_num: int, line_text: str) -> int:
    try:
        return int(token, 0)
    except ValueError:
        raise AssemblerError(f"Invalid integer literal '{token}'", line_num, line_text)


def _encode_delay_sideset(delay: int, side: int | None, side_set_count: int, side_set_opt: bool,
                          line_num: int, line_text: str) -> int:
    total_ss_bits = side_set_count + (1 if side_set_opt else 0)
    delay_bits = 5 - total_ss_bits
    if delay_bits < 0:
        raise AssemblerError(f"Invalid side-set configuration: {total_ss_bits} bits exceeds 5-bit field",
                             line_num, line_text)
    max_delay = (1 << delay_bits) - 1
    if not (0 <= delay <= max_delay):
        raise AssemblerError(f"Delay ({delay}) exceeds maximum allowed ({max_delay}) with "
                             f"{side_set_count} side-set bits", line_num, line_text)
    val = delay
    if side_set_count > 0:
        max_side = (1 << side_set_count) - 1
        if side is None and not side_set_opt:
            side = 0
        if side is not None:
            if not (0 <= side <= max_side):
                raise AssemblerError(f"Side-set value ({side}) out of range (0-{max_side})", line_num, line_text)
            ss_field = ((1 << side_set_count) if side_set_opt else 0) | side
            val |= ss_field << delay_bits
    elif side is not None:
        raise AssemblerError("Side-set value specified but .side_set is 0", line_num, line_text)
    return val & 0x1F


def disassemble(word: int, side_set_count: int = 0, side_set_opt: bool = False) -> str:
    """Disassemble a 16-bit instruction word into assembly text."""
    word &= 0xFFFF
    opcode = word >> 13
    delay_ss = (word >> 8) & 0x1F
    args = word & 0xFF

    delay_bits = 5 - (side_set_count + (1 if side_set_opt and side_set_count else 0))
    delay = delay_ss & ((1 << delay_bits) - 1)
    side = None
    if side_set_count > 0:
        ss_field = delay_ss >> delay_bits
        if not side_set_opt or (ss_field >> side_set_count) & 1:
            side = ss_field & ((1 << side_set_count) - 1)
    suffix = (f" side {side}" if side is not None else "") + (f" [{delay}]" if delay else "")

    if opcode >> 1 == OP_JMP >> 1:
        cond = (args >> 5) & 7
        target = ((opcode & 1) << 5) | (args & 0x1F)
        if cond == JC_NEVER:
            return f"nop{suffix}"
        cond_str = ["", "never", "x--", "y--", "cond0", "cond1", "cond2", "cond3"][cond]
        return f"jmp {cond_str + ', ' if cond_str else ''}{target}{suffix}"
    if opcode in (OP_IN, OP_OUT):
        operand = (args >> 5) & 7
        count = (args & 0xF) or 16
        auto = " auto" if args & 0x10 else ""
        names = IN_SRC if opcode == OP_IN else OUT_DEST
        name = next((n for n, v in names.items() if v == operand), "reserved")
        return f"{'in' if opcode == OP_IN else 'out'} {name}, {count}{auto}{suffix}"
    if opcode >> 1 == OP_ALU >> 1:
        op = ALU_OPS[((opcode & 1) << 2) | ((args >> 3) & 3)]
        dest = next((n for n, v in ALU_DEST.items() if v == (args >> 5) & 7), "reserved")
        src = next((n for n, v in ALU_SRC.items() if v == args & 7), "reserved")
        return f"{op} {dest}, {src}{suffix}"
    if opcode == OP_CTRL:
        ctype, a = args >> 6, args & 0x3F
        if ctype == CT_IRQ:
            parts = ["irq"] + [n for n, b in (("clear", 5), ("wait", 4), ("peer", 3)) if a >> b & 1]
            return f"{' '.join(parts)} {a & 7}{suffix}"
        if ctype == CT_FIFO:
            is_pull = a >> 5 & 1
            parts = ["pull" if is_pull else "push"]
            if not a >> 4 & 1:
                parts.append("noblock")
            if a >> 3 & 1:
                parts.append("ifempty" if is_pull else "iffull")
            if a >> 2 & 1:
                parts.append("peer")
            return f"{' '.join(parts)}{suffix}"
        if ctype == CT_WAIT:
            pol, edge, t_en, slot = a >> 5 & 1, a >> 4 & 1, a >> 3 & 1, a & 3
            return (f"wait {pol} cond{slot}{' edge' if edge else ''}"
                    f"{' timeout' if t_en else ''}{suffix}")
        action = next(n for n, v in PAU_ACTIONS.items() if v == a >> 4)
        if action == "crc_feed":
            return f"pau crc_feed, {a & 0xF or 16}{suffix}"
        return f"pau {action}{suffix}" if not a & 0xF else f".word 0x{word:04x}"
    dest = (args >> 5) & 7
    return f"set {REV_SET_DEST[dest]}, {args & 0x1F}{suffix}"


class _Conditions:
    """Allocates COND0..COND3 for the conditions a program uses (ISA.md, Conditions).

    Explicit .condN directives are fixed; others take the slot that already holds them, then a
    slot whose reset value matches, then the first free slot.
    """

    def __init__(self) -> None:
        self.cfg = list(COND_RESET)
        self.used = [False] * 4
        self.pin_slot: int | None = None

    def fix(self, slot: int, c: int) -> None:
        self.cfg[slot] = c
        self.used[slot] = True

    def slot_for(self, c: int, any_polarity: bool, line_num: int, text: str, pin: bool = False) -> tuple[int, int]:
        """Slot holding condition c; with any_polarity also an inverted copy. Returns (slot, inverted)."""
        if not pin:
            for s in range(4):
                if self.used[s] and s != self.pin_slot:
                    if self.cfg[s] == c:
                        return s, 0
                    if any_polarity and self.cfg[s] ^ 0x80 == c:
                        return s, 1
        elif self.pin_slot is not None:
            return self.pin_slot, (self.cfg[self.pin_slot] ^ c) >> 7
        order = [s for s in range(4) if COND_RESET[s] == c] + list(range(4))
        for s in order:
            if not self.used[s]:
                self.fix(s, c)
                if pin:
                    self.pin_slot = s
                return s, 0
        raise AssemblerError("More than 4 different conditions in one program", line_num, text)


def _parse_cond(tokens: list[str], line_num: int, text: str) -> tuple[int | None, int, bool]:
    """Condition tokens -> (condition byte or None for an explicit slot, slot, is `pin`)."""
    first = tokens[0].lower()
    if len(tokens) == 1 and re.fullmatch(r"cond[0-3]", first):
        return None, int(first[4]), False
    if len(tokens) == 1 and first in NAMED_CONDS:
        return cond_byte(*NAMED_CONDS[first]), 0, False
    inv = 0
    if first.startswith("!"):
        inv, first = 1, first[1:]
    if len(tokens) == 1 and first == "pin":
        return cond_byte(inv, CS_GPIO, 0), 0, True
    if len(tokens) != 2 or first not in COND_SRC:
        raise AssemblerError(f"Invalid condition '{' '.join(tokens)}'", line_num, text)
    src = COND_SRC[first]
    names = STATUS_NAMES if src == CS_STATUS else PAU_STATUS_NAMES if src == CS_PAU else {}
    idx_tok = tokens[1].lower()
    idx = names[idx_tok] if idx_tok in names else _parse_int(idx_tok, line_num, text)
    limit = 15 if src in (CS_XBIT, CS_YBIT, CS_STATUS, CS_PAU) else 7
    if not 0 <= idx <= limit:
        raise AssemblerError(f"Condition index out of range (0-{limit}): {idx}", line_num, text)
    return cond_byte(inv, src, idx), 0, False


def _kv(parts: list[str], line_num: int, text: str) -> dict[str, str]:
    out = {}
    for p in parts:
        key, _, val = p.partition("=")
        out[key.lower()] = val.lower()
    return out


def assemble(source_code: str) -> Program:
    """Assemble source code text into a Program (list of 16-bit words, max 64)."""
    lines = source_code.splitlines()
    cfg: dict = {}
    prog_name = "program"
    side_set_count, side_set_opt = 0, False
    conds = _Conditions()
    labels: dict[str, int] = {}
    instructions: list[tuple[int, str, int]] = []
    current_addr = 0

    # Pass 1: directives, labels, instruction text
    for line_num, raw_line in enumerate(lines, 1):
        code_part = re.split(r";|//|#", raw_line, maxsplit=1)[0].strip()
        if not code_part:
            continue
        if code_part.startswith("."):
            parts = code_part.split()
            directive = parts[0].lower()
            if directive == ".program":
                prog_name = parts[1] if len(parts) > 1 else prog_name
            elif directive == ".side_set":
                if len(parts) < 2:
                    raise AssemblerError(".side_set requires bit count argument", line_num, raw_line)
                side_set_count = _parse_int(parts[1], line_num, raw_line)
                if not 0 <= side_set_count <= 5:
                    raise AssemblerError("Side-set bit count must be between 0 and 5", line_num, raw_line)
                side_set_opt = any(p.lower() in ("opt", "optional") for p in parts[2:])
                cfg["side_set_count"], cfg["side_set_opt"] = side_set_count, side_set_opt
                cfg["side_set_pindirs"] = any(p.lower() in ("pindirs", "pindir") for p in parts[2:])
            elif directive in (".open_drain", ".opendrain"):
                if len(parts) < 2:
                    raise AssemblerError(".open_drain requires mask or pin list", line_num, raw_line)
                arg_str = "".join(parts[1:])
                if "," in arg_str:
                    mask = sum(1 << _parse_int(p, line_num, raw_line) for p in arg_str.split(",") if p)
                else:
                    mask = _parse_int(arg_str, line_num, raw_line)
                cfg["open_drain_mask"], cfg["has_explicit_open_drain"] = mask & 0xFF, True
            elif directive in (".clkdiv", ".prescaler"):
                if len(parts) < 2:
                    raise AssemblerError(f"{directive} requires divider argument", line_num, raw_line)
                cfg["clkdiv_int"] = _parse_int(parts[1], line_num, raw_line)
                if len(parts) > 2:
                    cfg["clkdiv_frac"] = _parse_int(parts[2], line_num, raw_line) & 0xFF
                cfg["has_explicit_clkdiv"] = True
            elif directive in (".push_thresh", ".pull_thresh"):
                n = _parse_int(parts[1], line_num, raw_line)
                if not 1 <= n <= 16:
                    raise AssemblerError("Threshold must be 1..16", line_num, raw_line)
                cfg[directive[1:]] = n & 0xF
            elif re.fullmatch(r"\.cond[0-3]", directive):
                c, _, _ = _parse_cond(parts[1:], line_num, raw_line)
                if c is None:
                    raise AssemblerError("A condition cannot be another slot", line_num, raw_line)
                conds.fix(int(directive[5]), c)
            elif directive == ".pau":
                kv = _kv(parts[1:], line_num, raw_line)
                if kv.get("enc") not in PAU_ENC:
                    raise AssemblerError(f".pau enc= one of {list(PAU_ENC)}", line_num, raw_line)
                mode = PAU_ENC[kv["enc"]] | ("inv" in kv) << 2 | ("diff" in kv) << 3
                if "stuff" in kv:
                    n = _parse_int(kv["stuff"], line_num, raw_line)
                    if not 1 <= n <= 16 or kv.get("stuff_mode", "ones") not in STUFF_MODES:
                        raise AssemblerError("stuff=1..16, stuff_mode=ones|zeros|equal", line_num, raw_line)
                    mode |= (n & 0xF) << 4 | STUFF_MODES[kv.get("stuff_mode", "ones")] << 8
                if "baud" in kv:
                    mode |= (_parse_int(kv["baud"], line_num, raw_line) & 0x1F) << 10
                cfg["pau_mode"] = mode
            elif directive == ".crc":
                kv = _kv(parts[1:], line_num, raw_line)
                width = _parse_int(kv.get("width", "0"), line_num, raw_line)
                if not 1 <= width <= 32 or "poly" not in kv:
                    raise AssemblerError(".crc width=1..32 poly=<value> [init=ones] [xorout=1] [res=<value>]",
                                         line_num, raw_line)
                cfg["crc_cfg"] = (width - 1) | (kv.get("init") == "ones") << 5 | (kv.get("xorout") == "1") << 6
                cfg["crc_poly"] = _parse_int(kv["poly"], line_num, raw_line) & 0xFFFFFFFF
                cfg["crc_res"] = _parse_int(kv.get("res", "0"), line_num, raw_line) & 0xFFFFFFFF
            elif directive == ".wrap_target":
                cfg["wrap_bottom"] = current_addr
            elif directive == ".wrap":
                cfg["wrap_top"] = max(0, current_addr - 1)
            elif directive == ".word":
                if len(parts) < 2:
                    raise AssemblerError(".word requires a value argument", line_num, raw_line)
                instructions.append((current_addr, code_part, line_num))
                current_addr += 1
            else:
                raise AssemblerError(f"Unknown directive '{directive}'", line_num, raw_line)
            continue

        while (m := re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*(.*)$", code_part)):
            label = m.group(1).lower()
            if label in labels:
                raise AssemblerError(f"Duplicate label '{label}'", line_num, raw_line)
            labels[label] = current_addr
            code_part = m.group(2).strip()
        if not code_part:
            continue
        if current_addr >= IMEM_WORDS:
            raise AssemblerError(f"Program exceeds {IMEM_WORDS} instructions", line_num, raw_line)
        instructions.append((current_addr, code_part, line_num))
        current_addr += 1

    # Pass 2: encode
    words: list[int] = []
    for _addr, text, line_num in instructions:
        if text.startswith(".word"):
            words.append(_parse_int(text.split()[1], line_num, text) & 0xFFFF)
            continue

        delay = 0
        if (m := re.search(r"\[\s*([0-9a-zA-Z_]+)\s*\]", text)):
            delay = _parse_int(m.group(1), line_num, text)
            text = text[:m.start()] + text[m.end():]
        side = None
        if (m := re.search(r"\bside\s+([0-9a-zA-Z_]+)\b", text, re.IGNORECASE)):
            side = _parse_int(m.group(1), line_num, text)
            text = text[:m.start()] + text[m.end():]
        dss = _encode_delay_sideset(delay, side, side_set_count, side_set_opt, line_num, text) << 8

        clean = text.strip()
        tokens = [t for t in re.split(r"[\s,]+", clean) if t]
        if not tokens:
            raise AssemblerError("Empty instruction line", line_num, text)
        mnem, args = tokens[0].lower(), tokens[1:]

        def target_of(tok: str) -> int:
            t = labels[tok.lower()] if tok.lower() in labels else _parse_int(tok, line_num, clean)
            if not 0 <= t < IMEM_WORDS:
                raise AssemblerError(f"JMP target out of range (0-{IMEM_WORDS - 1}): {t}", line_num, clean)
            return t

        def jmp(cond_code: int, target: int) -> int:
            return ((OP_JMP | target >> 5) << 13) | dss | cond_code << 5 | (target & 0x1F)

        if mnem == "nop":
            if args:
                raise AssemblerError("NOP takes no arguments", line_num, clean)
            words.append(jmp(JC_NEVER, 0))

        elif mnem == "jmp":
            if not args:
                raise AssemblerError("JMP expects: jmp [condition,] target", line_num, clean)
            target = target_of(args[-1])
            cond_tokens = args[:-1]
            plain = {"": JC_ALWAYS, "always": JC_ALWAYS, "never": JC_NEVER,
                     "x--": JC_XDEC, "x-": JC_XDEC, "y--": JC_YDEC, "y-": JC_YDEC}
            key = cond_tokens[0].lower() if len(cond_tokens) == 1 else ("" if not cond_tokens else None)
            if key in plain:
                words.append(jmp(plain[key], target))
            else:
                c, slot, pin = _parse_cond(cond_tokens, line_num, clean)
                if c is not None:
                    slot, inverted = conds.slot_for(c, False, line_num, clean, pin)
                    if inverted:
                        raise AssemblerError("`pin` used with both polarities in JMP", line_num, clean)
                words.append(jmp(4 + slot, target))

        elif mnem == "wait":
            if len(args) < 2:
                raise AssemblerError("WAIT expects: wait <polarity> <condition> [edge] [timeout]", line_num, clean)
            pol_tok = args[0].lower()
            pol, edge = {"1": (1, 0), "high": (1, 0), "hi": (1, 0), "0": (0, 0), "low": (0, 0),
                         "lo": (0, 0), "rise": (1, 1), "rising": (1, 1), "fall": (0, 1),
                         "falling": (0, 1)}.get(pol_tok, (None, 0))
            if pol is None:
                raise AssemblerError(f"Invalid WAIT polarity: '{pol_tok}'", line_num, clean)
            rest = args[1:]
            t_en = 0
            while rest and rest[-1].lower() in ("edge", "level", "timeout", "t_en", "tout"):
                flag = rest.pop().lower()
                if flag == "edge":
                    edge = 1
                elif flag == "level":
                    edge = 0
                else:
                    t_en = 1
            c, slot, pin = _parse_cond(rest, line_num, clean)
            if c is not None:
                slot, inverted = conds.slot_for(c & 0x7F, True, line_num, clean, pin)
                pol ^= inverted ^ (c >> 7)
            a = pol << 5 | edge << 4 | t_en << 3 | slot
            words.append((OP_CTRL << 13) | dss | CT_WAIT << 6 | a)

        elif mnem in ("in", "out"):
            auto = 0
            if args and args[-1].lower() == "auto":
                auto, args = 1, args[:-1]
            if len(args) != 2:
                raise AssemblerError(f"{mnem.upper()} requires: {mnem} <operand>, <count> [auto]", line_num, clean)
            table = IN_SRC if mnem == "in" else OUT_DEST
            name = args[0].lower()
            if name not in table:
                raise AssemblerError(f"Invalid {mnem.upper()} operand: '{name}'", line_num, clean)
            count = _parse_int(args[1], line_num, clean)
            if not 1 <= count <= 16:
                raise AssemblerError(f"{mnem.upper()} bit count out of range (1-16): {count}", line_num, clean)
            op = OP_IN if mnem == "in" else OP_OUT
            words.append((op << 13) | dss | table[name] << 5 | auto << 4 | (count & 0xF))

        elif mnem in ALU_OPS or mnem == "not":
            if mnem == "not":
                if len(args) != 1:
                    raise AssemblerError("NOT syntax: not <dest>", line_num, clean)
                mnem, args = "xnor", [args[0], "null"]
            if len(args) != 2:
                raise AssemblerError(f"{mnem} syntax: {mnem} <dest>, <src>", line_num, clean)
            dest, src = args[0].lower(), args[1].lower()
            if dest not in ALU_DEST:
                raise AssemblerError(f"Invalid ALU destination: '{dest}'", line_num, clean)
            if src not in ALU_SRC:
                raise AssemblerError(f"Invalid ALU source: '{src}'", line_num, clean)
            op = ALU_OPS.index(mnem)
            words.append(((OP_ALU | op >> 2) << 13) | dss | ALU_DEST[dest] << 5 | (op & 3) << 3 | ALU_SRC[src])

        elif mnem == "irq":
            bits, idx = 0, None
            for a in args:
                al = a.lower()
                if al in ("clear", "clr"):
                    bits |= 1 << 5
                elif al in ("wait", "stall"):
                    bits |= 1 << 4
                elif al in ("peer", "peersm", "peer_sm"):
                    bits |= 1 << 3
                elif al in ("set", "cont", "nowait", "local"):
                    pass
                elif idx is None:
                    idx = _parse_int(a, line_num, clean)
                else:
                    raise AssemblerError(f"Unexpected extra argument in IRQ: '{a}'", line_num, clean)
            if idx is None or not 0 <= idx <= 7:
                raise AssemblerError("IRQ instruction requires an IRQ index (0-7)", line_num, clean)
            words.append((OP_CTRL << 13) | dss | CT_IRQ << 6 | bits | idx)

        elif mnem in ("push", "pull"):
            is_pull, block, ifcond, peer = int(mnem == "pull"), 1, 0, 0
            for a in args:
                al = a.lower()
                if al == "peer":
                    peer = 1
                elif al in ("block", "blocking"):
                    block = 1
                elif al in ("noblock", "nonblock", "nonblocking"):
                    block = 0
                elif al in ("ifcond", "cond") or al in (("ifempty", "if_empty") if is_pull else ("iffull", "if_full")):
                    ifcond = 1
                else:
                    raise AssemblerError(f"Invalid argument for {mnem}: '{a}'", line_num, clean)
            words.append((OP_CTRL << 13) | dss | CT_FIFO << 6 | is_pull << 5 | block << 4 | ifcond << 3
                         | peer << 2)

        elif mnem == "pau":
            action = args[0].lower() if args else ""
            count = 0
            if action == "crc_feed" and len(args) == 2:
                count = _parse_int(args[1], line_num, clean)
                if not 1 <= count <= 16:
                    raise AssemblerError("CRC feed count out of range (1-16)", line_num, clean)
            elif action not in PAU_ACTIONS or len(args) != 1:
                raise AssemblerError(f"PAU action: one of {list(PAU_ACTIONS)}; crc_feed takes a count",
                                     line_num, clean)
            words.append((OP_CTRL << 13) | dss | CT_PAU << 6 | PAU_ACTIONS[action] << 4 | (count & 0xF))

        elif mnem in ("set", "cfg"):
            if len(args) < 2:
                raise AssemblerError(f"{mnem} requires: {mnem} <dest>, <val>", line_num, clean)
            dest = args[0].lower()
            if dest not in SET_DEST:
                raise AssemblerError(f"Invalid SET destination: '{dest}'", line_num, clean)
            val_str = "".join(args[1:]).lower()
            if SET_DEST[dest] == 7:
                val = 0
                for part in val_str.split("|"):
                    val |= PAU_CTL_FLAGS[part] if part in PAU_CTL_FLAGS else _parse_int(part, line_num, clean)
            elif val_str in labels:                     # an address, e.g. a return point for OUT PC
                val = labels[val_str]
            else:
                val = _parse_int(val_str, line_num, clean)
            if not 0 <= val <= 31:
                raise AssemblerError(f"SET value out of range (0-31): {val}", line_num, clean)
            words.append((OP_SET << 13) | dss | SET_DEST[dest] << 5 | val)

        else:
            raise AssemblerError(f"Unknown instruction mnemonic: '{mnem}'", line_num, clean)

    cfg.setdefault("wrap_bottom", 0)
    cfg.setdefault("wrap_top", max(0, current_addr - 1))
    return Program(words, name=prog_name, cond_cfg=conds.cfg, pin_slot=conds.pin_slot, **cfg)


def main() -> None:
    """CLI entry point for asm.py."""
    parser = argparse.ArgumentParser(description="openPIO Assembler (ISA v2)")
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

    out_path = args.output or args.input.rsplit(".", 1)[0] + ".bin"
    with open(out_path, "wb") as f:
        f.write(words.to_bytes())
    print(f"Assembled {len(words)} instructions to {out_path}")
    for s, c in enumerate(words.cond_cfg):
        print(f"  cond{s} = 0x{c:02x}  {cond_text(c)}")


if __name__ == "__main__":
    main()
