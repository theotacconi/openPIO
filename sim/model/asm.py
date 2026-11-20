"""asm.py - Assembler for the openPIO ISA v3 (ISA.md).

Converts assembly text into 16-bit instruction words (max 64, one IMEM page), plus the per-SM configuration
the program needs as Program.cfg: every register host.Host.load writes comes from the source.
Configuration directives (a field not set keeps its reset value, HOST.md):

    .side_set <n> [opt] [pindirs]   SIDESET count, opt, pindirs
    .sideset_pins <pin>             SIDESET base
    .in_pins / .out_pins <pin>      PINBASE in_base / out_base
    .set_pins <pin> [<count>]       PINBASE set_base, set_count (1..5)
    .pin_mask <pin>, <pin> ...      PIN_MASK
    .open_drain <pin>, <pin> ...    GPIO open drain (chip-wide: the host adds these pins)
    .shift_in / .shift_out left|right, .push_thresh / .pull_thresh <1..16>       MISC
    .clkdiv <int> [<frac>]          CLKDIV
    .fifo_split <1..31>             FIFO_SPLIT: TX ring 8 n words, RX ring 256 - 8 n (16: 128 each)
    .cond0 .. .cond3 <condition>    COND_CFG (other conditions get a free slot)
    .pau ..., .crc ...   PAU_MODE, PAU_CODE, PAU_CFG, CRC_CFG, CRC_POLY, CRC_RES
    .wrap_target, .wrap             WRAP
    .start <label>, .x <v>, .y <v>  PC, X, Y preset after RESET
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


IMEM_WORDS = 256    # four pages of PAGE_WORDS
PAGE_WORDS = 64     # a program: JMP and OUT PC stay in their page, so it runs in any page

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
JC_ALWAYS, JC_FAR, JC_XDEC, JC_YDEC = 0, 1, 2, 3     # 4..7 = COND0..COND3; FAR reserved: a NOP

# Operand codes (IN source, OUT destination, ALU source / destination)
R_PINS, R_PINDIRS, R_X, R_Y, R_ISR, R_OSR, R_NULL, R_SPECIAL = range(8)
OPERAND_NAMES = ["pins", "pindirs", "x", "y", "isr", "osr", "null", "special"]
IN_SRC = {"pins": R_PINS, "time": R_PINDIRS, "x": R_X, "y": R_Y, "isr": R_ISR, "osr": R_OSR,
          "null": R_NULL, "status": R_SPECIAL}
OUT_DEST = {"pins": R_PINS, "pindirs": R_PINDIRS, "x": R_X, "y": R_Y, "isr": R_ISR,
            "pau_baud": R_OSR, "null": R_NULL, "pc": R_SPECIAL}
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
                "osr_empty": 4, "isr_full": 5, "tx_empty": 6, "rx_full": 7, "pins_changed": 8}
PAU_STATUS_NAMES = {"tx_idle": 0, "rx_idle": 1, "tx_ready": 2, "rx_ready": 3,
                    "crc_ok": 4, "stuff_err": 6, "overrun": 7,
                    "line_00": 8, "line_01": 9, "line_10": 10, "line_11": 11,   # in_base + 1, in_base
                    "bit_err": 12}

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
JMP_PLAIN = {"": JC_ALWAYS, "always": JC_ALWAYS,
             "x--": JC_XDEC, "x-": JC_XDEC, "y--": JC_YDEC, "y-": JC_YDEC}

# PAU line codes (ISA.md, Line code): PAU_CODE values
PAU_CODES = {"nrz": 0x00FA, "nrzi": 0x0005, "manchester": 0xFAAF, "bmc": 0x5055,
             "biphase_mark": 0x5055, "fm0": 0x0555, "diff_manchester": 0x5505, "miller": 0x5001,
             "mfm": 0x5001}
# A named code's own alignment and run (ISA.md, Receiver): Manchester's first edge is mid-bit,
# Miller (MFM) holds a level for up to 4 half-bits; `align=` and `run=` override them.
PAU_CODE_RX = {"manchester": ("mid", 2), "diff_manchester": ("mid", 2), "miller": ("edge", 4),
               "mfm": ("edge", 4)}
PIN2_ROLES = {"none": 0, "complement": 1, "clock": 2}
STUFF_MODES = {"ones": 0, "equal": 1}
NOP_WORD = (OP_ALU << 13) | R_NULL << 5 | R_NULL      # mov null, null


def cond_byte(inv: int, src: int, idx: int) -> int:
    return ((inv & 1) << 7) | ((src & 7) << 4) | (idx & 0xF)


def cond_text(c: int) -> str:
    src, idx = (c >> 4) & 7, c & 0xF
    names = STATUS_NAMES if src == CS_STATUS else PAU_STATUS_NAMES if src == CS_PAU else {}
    idx_str = next((n for n, v in names.items() if v == idx), str(idx))
    return f"{'!' if c & 0x80 else ''}{REV_COND_SRC[src]} {idx_str}"


# Configuration of an SM at reset (HOST.md), by Program field
CONFIG_RESET = dict(
    wrap_bottom=0, wrap_top=IMEM_WORDS - 1, side_set_count=0, side_set_opt=False,
    side_set_pindirs=False, sideset_base=0, in_base=0, out_base=0, set_base=0, set_count=5,
    pin_mask=0xFFFF, shift_in_right=True, shift_out_right=True, push_thresh=0, pull_thresh=0,
    clkdiv_int=1, clkdiv_frac=0, cond_cfg=tuple(COND_RESET), pau_mode=0, pau_code=0x00FA,
    pau_cfg=0, crc_cfg=0, crc_poly=0, crc_res=0, fifo_split=0, open_drain_mask=0, start=0, x=0,
    y=0)


class Program(list[int]):
    """Assembled program: a list of 16-bit words with the configuration it needs.

    cfg: the CONFIG_RESET fields the source sets (wrap and cond_cfg always); start, x and y are
    preset by the host after RESET. pin_slot: slot of the `pin` condition, whose pin a loader
    may override (jmp_pin).
    """

    def __init__(self, words: list[int], name: str = "program", pin_slot: int | None = None,
                 **cfg) -> None:
        super().__init__(words)
        self.name = name
        self.pin_slot = pin_slot
        self.cfg = cfg

    def config(self) -> dict:
        """Every configuration field: the program's over the reset values."""
        return {**CONFIG_RESET, **self.cfg}

    def to_bytes(self) -> bytes:
        """Return raw little-endian 16-bit words."""
        return struct.pack(f"<{len(self)}H", *self)


class _At:
    """A source line, for the errors found on it."""

    def __init__(self, line_num: int, text: str) -> None:
        self.line_num, self.text = line_num, text

    def error(self, message: str) -> AssemblerError:
        return AssemblerError(message, self.line_num, self.text)

    def int(self, token: str) -> int:
        try:
            return int(token, 0)
        except ValueError:
            raise self.error(f"Invalid integer literal '{token}'") from None

    def int_in(self, token: str, lo: int, hi: int, message: str) -> int:
        """An integer from lo to hi; `message`, with {} for the value, if out of range."""
        n = self.int(token)
        if not lo <= n <= hi:
            raise self.error(message.format(n))
        return n


def _encode_delay_sideset(delay: int, side: int | None, side_set_count: int, side_set_opt: bool,
                          at: _At) -> int:
    total_ss_bits = side_set_count + (1 if side_set_opt else 0)
    delay_bits = 5 - total_ss_bits
    if delay_bits < 0:
        raise at.error(f"Invalid side-set configuration: {total_ss_bits} bits exceeds 5-bit field")
    max_delay = (1 << delay_bits) - 1
    if not (0 <= delay <= max_delay):
        raise at.error(f"Delay ({delay}) exceeds maximum allowed ({max_delay}) with "
                       f"{side_set_count} side-set bits")
    val = delay
    if side_set_count > 0:
        max_side = (1 << side_set_count) - 1
        if side is None and not side_set_opt:
            side = 0
        if side is not None:
            if not (0 <= side <= max_side):
                raise at.error(f"Side-set value ({side}) out of range (0-{max_side})")
            ss_field = ((1 << side_set_count) if side_set_opt else 0) | side
            val |= ss_field << delay_bits
    elif side is not None:
        raise at.error("Side-set value specified but .side_set is 0")
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

    if word & ~0x1F00 == NOP_WORD:
        return f"nop{suffix}"
    if opcode >> 1 == OP_JMP >> 1:
        cond = (args >> 5) & 7
        target = ((opcode & 1) << 5) | (args & 0x1F)
        if cond == JC_FAR:
            return f".word {word:#06x}"                     # reserved
        cond_str = ["", "far", "x--", "y--", "cond0", "cond1", "cond2", "cond3"][cond]
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
        if ctype == CT_FIFO and not a & 7:
            is_pull = a >> 5 & 1
            parts = ["pull" if is_pull else "push"]
            if not a >> 4 & 1:
                parts.append("noblock")
            if a >> 3 & 1:
                parts.append("ifempty" if is_pull else "iffull")
            return f"{' '.join(parts)}{suffix}"
        if ctype == CT_WAIT and not a & 4:
            pol, edge, t_en, slot = a >> 5 & 1, a >> 4 & 1, a >> 3 & 1, a & 3
            return (f"wait {pol} cond{slot}{' edge' if edge else ''}"
                    f"{' timeout' if t_en else ''}{suffix}")
        if ctype == CT_PAU:
            action = next(n for n, v in PAU_ACTIONS.items() if v == a >> 4)
            if action == "crc_feed":
                return f"pau crc_feed, {a & 0xF or 16}{suffix}"
            if not a & 0xF:
                return f"pau {action}{suffix}"
        return f".word 0x{word:04x}"                    # reserved: a NOP
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

    def slot_for(self, c: int, any_polarity: bool, at: _At, pin: bool = False) -> tuple[int, int]:
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
        raise at.error("More than 4 different conditions in one program")


def _parse_cond(tokens: list[str], at: _At) -> tuple[int | None, int, bool]:
    """Condition tokens -> (condition byte or None for an explicit slot, slot, is `pin`)."""
    first = tokens[0].lower() if tokens else ""
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
        raise at.error(f"Invalid condition '{' '.join(tokens)}'")
    src = COND_SRC[first]
    names = STATUS_NAMES if src == CS_STATUS else PAU_STATUS_NAMES if src == CS_PAU else {}
    idx_tok = tokens[1].lower()
    idx = names[idx_tok] if idx_tok in names else at.int(idx_tok)
    limit = 7 if src in (CS_IRQ, CS_PEER) else 15
    if not 0 <= idx <= limit:
        raise at.error(f"Condition index out of range (0-{limit}): {idx}")
    return cond_byte(inv, src, idx), 0, False


def condition(text: str) -> int:
    """The condition byte (ISA.md, Conditions) written as in a program, for a host that loads one
    at run time (COND_CFG, or a word a program moves into COND): condition("!gpio 12") == 0x8C."""
    at = _At(0, text)
    c, _, _ = _parse_cond(text.replace(",", " ").split(), at)
    if c is None:
        raise at.error("A condition, not a slot")
    return c


def _pins(parts: list[str], at: _At) -> int:
    """A list of pins `0, 2, 5` as a mask."""
    mask = 0
    for p in "".join(parts).split(","):
        mask |= 1 << at.int_in(p, 0, 15, "Pin out of range (0-15): {}")
    return mask


def _kv(parts: list[str]) -> dict[str, str]:
    """`key=value` arguments (a bare `key` has the value "")."""
    return {key.lower(): val.lower() for key, _, val in (p.partition("=") for p in parts)}


def _split(text: str) -> tuple[list[str], str | None, str | None]:
    """An instruction's text -> (mnemonic and arguments, delay, side-set value)."""
    found = []
    for pattern in (r"\[\s*([0-9a-zA-Z_]+)\s*\]", r"\bside\s+([0-9a-zA-Z_]+)\b"):
        m = re.search(pattern, text, re.IGNORECASE)
        found.append(m and m.group(1))
        if m:
            text = text[:m.start()] + text[m.end():]
    return [t for t in re.split(r"[\s,]+", text.strip()) if t], *found


BASE_PINS = {".in_pins": "in_base", ".out_pins": "out_base", ".sideset_pins": "sideset_base",
             ".set_pins": "set_base"}
WAIT_POLARITY = {"1": (1, 0), "high": (1, 0), "hi": (1, 0), "0": (0, 0), "low": (0, 0),
                 "lo": (0, 0), "rise": (1, 1), "rising": (1, 1), "fall": (0, 1), "falling": (0, 1)}


class _Assembler:
    """Pass 1 (line) reads directives, labels and instruction text; pass 2 (finish) gives the
    JMP conditions their slots, then encodes every instruction. Each directive and mnemonic has
    a method, found in DIRECTIVES and MNEMONICS at the end of the class."""

    def __init__(self) -> None:
        self.cfg: dict = {}
        self.name = "program"
        self.side_set_count, self.side_set_opt = 0, False
        self.conds = _Conditions()
        self.labels: dict[str, int] = {}
        self.lines: list[tuple[str, _At]] = []     # one per word: its text and source line
        self.start: tuple[str, _At] | None = None

    # Pass 1

    def line(self, line_num: int, raw: str) -> None:
        at = _At(line_num, raw)
        code = re.split(r";|//|#", raw, maxsplit=1)[0].strip()
        if code.startswith("."):
            parts = code.split()
            directive = self.DIRECTIVES.get(parts[0].lower())
            if directive is None:
                raise at.error(f"Unknown directive '{parts[0].lower()}'")
            directive(self, parts[0].lower(), parts[1:], at)
            return
        while (m := re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*(.*)$", code)):
            label = m.group(1).lower()
            if label in self.labels:
                raise at.error(f"Duplicate label '{label}'")
            self.labels[label] = len(self.lines)
            code = m.group(2).strip()
        if code:
            self.word(code, at)

    def word(self, text: str, at: _At) -> None:
        if len(self.lines) >= PAGE_WORDS:
            raise at.error(f"Program exceeds {PAGE_WORDS} instructions")
        self.lines.append((text, at))

    def d_program(self, d, args, at):
        if args:
            self.name = args[0]

    def d_side_set(self, d, args, at):
        if not args:
            raise at.error(".side_set requires bit count argument")
        self.side_set_count = at.int_in(args[0], 0, 5, "Side-set bit count must be between 0 and 5")
        flags = {a.lower() for a in args[1:]}
        self.side_set_opt = bool(flags & {"opt", "optional"})
        self.cfg.update(side_set_count=self.side_set_count, side_set_opt=self.side_set_opt,
                        side_set_pindirs=bool(flags & {"pindirs", "pindir"}))

    def d_pin_list(self, d, args, at):
        if not args:
            raise at.error(f"{d} requires a pin list")
        self.cfg["pin_mask" if d == ".pin_mask" else "open_drain_mask"] = _pins(args, at)

    def d_base(self, d, args, at):
        message = f"{d} requires a base pin 0..15"
        if not args:
            raise at.error(message)
        self.cfg[BASE_PINS[d]] = at.int_in(args[0], 0, 15, message)
        if d == ".set_pins" and len(args) > 1:
            self.cfg["set_count"] = at.int_in(args[1], 1, 5, "SET pin count must be 1..5")

    def d_shift(self, d, args, at):
        if len(args) != 1 or args[0].lower() not in ("left", "right"):
            raise at.error(f"{d} left|right")
        self.cfg[d[1:] + "_right"] = args[0].lower() == "right"

    def d_preset(self, d, args, at):
        if len(args) != 1:
            raise at.error(f"{d} requires a value")
        self.cfg[d[1:]] = at.int(args[0]) & 0xFFFF

    def d_start(self, d, args, at):
        if len(args) != 1:
            raise at.error(".start requires a label")
        self.start = args[0].lower(), at

    def d_clkdiv(self, d, args, at):
        if not args:
            raise at.error(".clkdiv requires a divider")
        self.cfg["clkdiv_int"] = at.int_in(args[0], 1, 0xFFFF, "CLKDIV integer part must be 1..65535")
        if len(args) > 1:
            self.cfg["clkdiv_frac"] = at.int(args[1]) & 0xFF

    def d_fifo_split(self, d, args, at):
        message = ".fifo_split takes 1..31 (the TX ring in 8 words)"
        if len(args) != 1:
            raise at.error(message)
        self.cfg["fifo_split"] = at.int_in(args[0], 1, 31, message)

    def d_thresh(self, d, args, at):
        if not args:
            raise at.error("Threshold must be 1..16")
        self.cfg[d[1:]] = at.int_in(args[0], 1, 16, "Threshold must be 1..16") & 0xF

    def d_cond(self, d, args, at):
        c, _, _ = _parse_cond(args, at)
        if c is None:
            raise at.error("A condition cannot be another slot")
        self.conds.fix(int(d[5]), c)

    def d_pau(self, d, args, at):
        # .pau [code=<name>|<value>] [inv] [pin2=none|complement|clock] [align=mid] [ext]
        #      [cpol=0|1] [cpha=0|1] [stuff=1..16 [stuff_mode=ones|equal]] [baud=0..31] [run=2..5]
        kv = _kv(args)
        code = kv.get("code", "nrz")
        self.cfg["pau_code"] = PAU_CODES[code] if code in PAU_CODES else at.int(code) & 0xFFFF
        align, run = PAU_CODE_RX.get(code, ("edge", 2))
        align, run = kv.get("align", align), kv.get("run", str(run))
        pin2 = kv.get("pin2", "none")
        if pin2 not in PIN2_ROLES or align not in ("edge", "mid"):
            raise at.error(f"pin2= one of {list(PIN2_ROLES)}, align=edge|mid")
        mode = (PIN2_ROLES[pin2] | ("inv" in kv) << 2 | (align == "mid") << 3 | ("ext" in kv) << 4
                | (kv.get("cpol") == "1") << 5 | (kv.get("cpha") == "1") << 6)
        if "stuff" in kv:
            n, stuff_mode = at.int(kv["stuff"]), kv.get("stuff_mode", "ones")
            if not 1 <= n <= 16 or stuff_mode not in STUFF_MODES:
                raise at.error("stuff=1..16, stuff_mode=ones|equal")
            mode |= STUFF_MODES[stuff_mode] << 7 | (n & 0xF) << 8
        self.cfg["pau_mode"] = mode
        run = at.int_in(run, 2, 5, "run=2..5")
        self.cfg["pau_cfg"] = at.int(kv.get("baud", "0")) & 0x1F | (run - 2) << 5

    def d_crc(self, d, args, at):
        kv = _kv(args)
        width = at.int(kv.get("width", "0"))
        if not 1 <= width <= 32 or "poly" not in kv:
            raise at.error(".crc width=1..32 poly=<value> [init=ones] [xorout=1] [res=<value>]")
        self.cfg["crc_cfg"] = (width - 1) | (kv.get("init") == "ones") << 5 | (kv.get("xorout") == "1") << 6
        # CRC_POLY and CRC_RES are MSB-aligned (ISA.md, PAU): the directive's values are not
        self.cfg["crc_poly"] = at.int(kv["poly"]) << (32 - width) & 0xFFFFFFFF
        self.cfg["crc_res"] = at.int(kv.get("res", "0")) << (32 - width) & 0xFFFFFFFF

    def d_wrap_target(self, d, args, at):
        self.cfg["wrap_bottom"] = len(self.lines)

    def d_wrap(self, d, args, at):
        self.cfg["wrap_top"] = max(0, len(self.lines) - 1)

    def d_word(self, d, args, at):
        if not args:
            raise at.error(".word requires a value argument")
        self.word(" ".join([d, *args]), at)

    # Pass 2

    def finish(self) -> Program:
        # JMP conditions take their slots first, in program order: a WAIT can use either polarity
        for text, at in self.lines:
            tokens = _split(text)[0]
            if tokens and tokens[0].lower() == "jmp" and len(tokens) > 2 and not (
                    len(tokens) == 3 and tokens[1].lower() in JMP_PLAIN):
                c, _, pin = _parse_cond(tokens[1:-1], at)
                if c is not None:
                    self.conds.slot_for(c, False, at, pin)
        words = [self.encode(text, at) for text, at in self.lines]
        if self.start is not None:
            label, at = self.start
            if label not in self.labels:
                raise at.error(f"Unknown label '{label}'")
            self.cfg["start"] = self.labels[label]
        self.cfg.setdefault("wrap_bottom", 0)
        self.cfg.setdefault("wrap_top", max(0, len(self.lines) - 1))
        return Program(words, name=self.name, cond_cfg=self.conds.cfg, pin_slot=self.conds.pin_slot,
                       **self.cfg)

    def encode(self, text: str, at: _At) -> int:
        if text.startswith(".word "):
            return at.int(text.split()[1]) & 0xFFFF
        tokens, delay, side = _split(text)
        dss = _encode_delay_sideset(at.int(delay) if delay else 0, side and at.int(side),
                                    self.side_set_count, self.side_set_opt, at)
        if not tokens:
            raise at.error("Empty instruction line")
        mnem, args = tokens[0].lower(), tokens[1:]
        if mnem not in self.MNEMONICS:
            raise at.error(f"Unknown instruction mnemonic: '{mnem}'")
        return self.MNEMONICS[mnem](self, mnem, args, at) | dss << 8

    def i_nop(self, mnem, args, at):
        if args:
            raise at.error("NOP takes no arguments")
        return NOP_WORD

    def i_jmp(self, mnem, args, at):
        if not args:
            raise at.error("JMP expects: jmp [condition,] target")
        tok = args[-1].lower()
        target = self.labels[tok] if tok in self.labels else at.int(args[-1])
        if not 0 <= target < PAGE_WORDS:
            raise at.error(f"JMP target out of range (0-{PAGE_WORDS - 1}): {target}")
        cond = args[:-1]
        key = cond[0].lower() if len(cond) == 1 else ("" if not cond else None)
        if key in JMP_PLAIN:
            code = JMP_PLAIN[key]
        else:
            c, slot, pin = _parse_cond(cond, at)
            if c is not None:
                slot, inverted = self.conds.slot_for(c, False, at, pin)
                if inverted:
                    raise at.error("`pin` used with both polarities in JMP")
            code = 4 + slot
        return (OP_JMP | target >> 5) << 13 | code << 5 | target & 0x1F

    def i_wait(self, mnem, args, at):
        if len(args) < 2:
            raise at.error("WAIT expects: wait <polarity> <condition> [edge] [timeout]")
        pol, edge = WAIT_POLARITY.get(args[0].lower(), (None, 0))
        if pol is None:
            raise at.error(f"Invalid WAIT polarity: '{args[0].lower()}'")
        rest, t_en = args[1:], 0
        while rest and rest[-1].lower() in ("edge", "level", "timeout", "t_en", "tout"):
            flag = rest.pop().lower()
            if flag in ("edge", "level"):
                edge = int(flag == "edge")
            else:
                t_en = 1
        c, slot, pin = _parse_cond(rest, at)
        if c is not None:
            slot, inverted = self.conds.slot_for(c & 0x7F, True, at, pin)
            pol ^= inverted ^ (c >> 7)
        return OP_CTRL << 13 | CT_WAIT << 6 | pol << 5 | edge << 4 | t_en << 3 | slot

    def i_in_out(self, mnem, args, at):
        auto = 0
        if args and args[-1].lower() == "auto":
            auto, args = 1, args[:-1]
        if len(args) != 2:
            raise at.error(f"{mnem.upper()} requires: {mnem} <operand>, <count> [auto]")
        table = IN_SRC if mnem == "in" else OUT_DEST
        name = args[0].lower()
        if name not in table:
            raise at.error(f"Invalid {mnem.upper()} operand: '{name}'")
        count = at.int_in(args[1], 1, 16, f"{mnem.upper()} bit count out of range (1-16): {{}}")
        op = OP_IN if mnem == "in" else OP_OUT
        return op << 13 | table[name] << 5 | auto << 4 | (count & 0xF)

    def i_alu(self, mnem, args, at):
        if mnem == "not":
            if len(args) != 1:
                raise at.error("NOT syntax: not <dest>")
            mnem, args = "xnor", [args[0], "null"]
        if len(args) != 2:
            raise at.error(f"{mnem} syntax: {mnem} <dest>, <src>")
        dest, src = args[0].lower(), args[1].lower()
        if dest not in ALU_DEST:
            raise at.error(f"Invalid ALU destination: '{dest}'")
        if src not in ALU_SRC:
            raise at.error(f"Invalid ALU source: '{src}'")
        op = ALU_OPS.index(mnem)
        return (OP_ALU | op >> 2) << 13 | ALU_DEST[dest] << 5 | (op & 3) << 3 | ALU_SRC[src]

    def i_irq(self, mnem, args, at):
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
                idx = at.int(a)
            else:
                raise at.error(f"Unexpected extra argument in IRQ: '{a}'")
        if idx is None or not 0 <= idx <= 7:
            raise at.error("IRQ instruction requires an IRQ index (0-7)")
        return OP_CTRL << 13 | CT_IRQ << 6 | bits | idx

    def i_fifo(self, mnem, args, at):
        is_pull, block, ifcond = int(mnem == "pull"), 1, 0
        for a in args:
            al = a.lower()
            if al in ("block", "blocking"):
                block = 1
            elif al in ("noblock", "nonblock", "nonblocking"):
                block = 0
            elif al in ("ifcond", "cond") or al in (("ifempty", "if_empty") if is_pull else ("iffull", "if_full")):
                ifcond = 1
            else:
                raise at.error(f"Invalid argument for {mnem}: '{a}'")
        return OP_CTRL << 13 | CT_FIFO << 6 | is_pull << 5 | block << 4 | ifcond << 3

    def i_pau(self, mnem, args, at):
        action = args[0].lower() if args else ""
        count = 0
        if action == "crc_feed" and len(args) == 2:
            count = at.int_in(args[1], 1, 16, "CRC feed count out of range (1-16)")
        elif action not in PAU_ACTIONS or len(args) != 1:
            raise at.error(f"PAU action: one of {list(PAU_ACTIONS)}; crc_feed takes a count")
        return OP_CTRL << 13 | CT_PAU << 6 | PAU_ACTIONS[action] << 4 | (count & 0xF)

    def i_set(self, mnem, args, at):
        if len(args) < 2:
            raise at.error(f"{mnem} requires: {mnem} <dest>, <val>")
        dest = args[0].lower()
        if dest not in SET_DEST:
            raise at.error(f"Invalid SET destination: '{dest}'")
        text = "".join(args[1:]).lower()
        if SET_DEST[dest] == 7:                     # PAU_CTL: flags or a number, joined by |
            val = 0
            for part in text.split("|"):
                val |= PAU_CTL_FLAGS[part] if part in PAU_CTL_FLAGS else at.int(part)
        elif text in self.labels:                   # an address, e.g. a return point for OUT PC
            val = self.labels[text]
        else:
            val = at.int(text)
        if not 0 <= val <= 31:
            raise at.error(f"SET value out of range (0-31): {val}")
        return OP_SET << 13 | SET_DEST[dest] << 5 | val

    DIRECTIVES = {".program": d_program, ".side_set": d_side_set, ".pin_mask": d_pin_list,
                  ".open_drain": d_pin_list, **dict.fromkeys(BASE_PINS, d_base),
                  ".shift_in": d_shift, ".shift_out": d_shift, ".x": d_preset, ".y": d_preset,
                  ".start": d_start, ".clkdiv": d_clkdiv, ".fifo_split": d_fifo_split,
                  ".push_thresh": d_thresh, ".pull_thresh": d_thresh,
                  **dict.fromkeys((f".cond{s}" for s in range(4)), d_cond), ".pau": d_pau, ".crc": d_crc,
                  ".wrap_target": d_wrap_target, ".wrap": d_wrap, ".word": d_word}
    MNEMONICS = {"nop": i_nop, "jmp": i_jmp, "wait": i_wait, "in": i_in_out, "out": i_in_out,
                 **dict.fromkeys(ALU_OPS + ["not"], i_alu), "irq": i_irq, "push": i_fifo,
                 "pull": i_fifo, "pau": i_pau, "set": i_set, "cfg": i_set}


def assemble(source_code: str) -> Program:
    """Assemble source code text into a Program (list of 16-bit words, max 64)."""
    a = _Assembler()
    for line_num, raw in enumerate(source_code.splitlines(), 1):
        a.line(line_num, raw)
    return a.finish()


def main() -> None:
    """CLI entry point for asm.py."""
    parser = argparse.ArgumentParser(description="openPIO Assembler (ISA v3)")
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
    for s, c in enumerate(words.config()["cond_cfg"]):
        print(f"  cond{s} = 0x{c:02x}  {cond_text(c)}")


if __name__ == "__main__":
    main()
