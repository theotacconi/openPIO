"""learn.py - find a protocol's parameters from a recording of the wire, and write the openPIO
programs that receive and send it.

openPIO's line layer is a small space of numbers (ISA.md, Principles): a protocol of its family
is one point in it. Learning is a search of that space against a recording:

    capture  -> edges of a pin (programs/logic/edges.asm on the chip, or any logic analyzer)
    infer    -> the parameters, each checked against every character of the recording
    generate -> RX and TX programs for those parameters, as assembler source

Two families so far:

- asynchronous serial characters (UART, RS-485, LIN, Modbus RTU, DMX, MIDI and the proprietary
  links built on them): bit time, polarity, data bits, parity, stop bits; RX and TX programs.
- self-clocked frames, bursts of edges between idle times (USB, CAN, HDLC, 10BASE-T, MFM,
  biphase mark, ...): the PAU's line code, polarity, half-bit, bit stuffing, the sync bits
  every frame starts with and the CRC; an RX program that passes the frames on with the CRC's
  verdict.
- clocked lines, a clock pin and data pins (SPI, SD card commands, ...): the clock, its mode
  (CPOL, CPHA), the select pin or the pauses that frame the data, sync bits, the CRC; an RX
  program through the PAU's external clock.

Next: pulse-width codes.

    python3 sim/learn/learn.py capture.json   # {"clock_hz": .., "level": 0|1, "edges": [t, ..]}
    python3 sim/learn/learn.py --frames capture.json
    python3 sim/learn/learn.py --clocked pins.json  # {"pins": {"<pin>": {"level", "edges"}, ..}}
"""
from __future__ import annotations

import argparse
import bisect
import itertools
import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "model"))
from asm import PAU_CODE_RX, PAU_CODES                   # noqa: E402


@dataclass
class Capture:
    """A pin's recording: its level before the first edge, then the clock of every edge."""
    level: int
    edges: list[int]

    @staticmethod
    def from_levels(levels: list[int], pin: int = 0) -> "Capture":
        """From a level per clock (a bench trace, a logic analyzer's samples), bit `pin`."""
        bits = [lv >> pin & 1 for lv in levels]
        return Capture(bits[0], [t for t in range(1, len(bits)) if bits[t] != bits[t - 1]])

    @staticmethod
    def from_edges_program(words: list[int], clocks_per_strobe: float = 1) -> "Capture":
        """From the RX words of programs/logic/edges.asm: the pins first, then 16-bit strobe
        times of the edges (wrapping: gaps under 65536 strobes)."""
        level, *times = words
        edges, t = [], 0
        for prev, cur in zip([times[0]] + times, times):
            t += (cur - prev) & 0xFFFF
            edges.append(round(t * clocks_per_strobe))
        return Capture(level & 1, edges)

    @staticmethod
    def from_record_program(words: list[int], pin: int, clocks_per_strobe: float = 1) -> "Capture":
        """Pin `pin` from the RX words of programs/logic/record.asm: the pins first, then
        (16-bit strobe time, pins) at every change of any pin."""
        level, edges, t, prev = words[0] >> pin & 1, [], 0, None
        cur = level
        for time, pins in zip(words[1::2], words[2::2]):
            t += (time - prev) & 0xFFFF if prev is not None else 0
            prev = time
            if (pins >> pin & 1) != cur:
                cur ^= 1
                edges.append(round(t * clocks_per_strobe))
        return Capture(level, edges)

    def widths(self) -> list[int]:
        """Clocks between consecutive edges."""
        return [b - a for a, b in zip(self.edges, self.edges[1:])]

    def level_at(self, t: float) -> int:
        """The level at clock t."""
        return self.level ^ (bisect.bisect_right(self.edges, t) & 1)


@dataclass
class AsyncSerial:
    """Asynchronous serial characters: a start bit, data LSB first, an optional parity bit,
    stop bits, idle between characters; `idle` is the line's level at rest (1: as UART)."""
    bit: float              # clocks a bit
    idle: int
    data_bits: int
    parity: str | None      # "even", "odd" or None
    stop_bits: int

    @property
    def length(self) -> int:
        """Bits a character takes, start to last stop bit."""
        return 1 + self.data_bits + (self.parity is not None) + self.stop_bits

    def __str__(self) -> str:
        p = {None: "N", "even": "E", "odd": "O"}[self.parity]
        return (f"async serial {self.data_bits}{p}{self.stop_bits}, {self.bit:.2f} clocks a bit, "
                f"{'idle high' if self.idle else 'idle low (inverted)'}")


# ---------------------------------------------------------------------------------- infer

def bit_time(widths: list[int]) -> float:
    """The bit time: every pulse lasts a whole number of bits. The narrowest pulses are one
    bit; the estimate is then refined over every pulse of up to 16 bits."""
    if not widths:
        raise ValueError("no edges: nothing to learn")
    shortest = sorted(widths)[:max(1, len(widths) // 10)]
    t = sum(shortest) / len(shortest)
    for _ in range(3):                                   # least squares over the bit counts
        counted = [(w, round(w / t)) for w in widths if 0 < round(w / t) <= 16]
        t = sum(w for w, _ in counted) / sum(n for _, n in counted)
    return t


def characters(cap: Capture, p: AsyncSerial) -> list[list[int]]:
    """The characters of a recording as bit lists (start bit first, logical levels), each
    sampled in the middle of its bits from its start edge."""
    out, i = [], 0
    while i < len(cap.edges):
        t0 = cap.edges[i]
        if cap.level_at(t0) == p.idle:                   # an edge back to idle: not a start
            i += 1
            continue
        bits = [cap.level_at(t0 + (k + 0.5) * p.bit) ^ (1 - p.idle) for k in range(p.length)]
        out.append(bits)
        end = t0 + (p.length - 0.5) * p.bit              # the middle of the last stop bit
        while i < len(cap.edges) and cap.edges[i] <= end:
            i += 1
    return out


def errors(chars: list[list[int]], p: AsyncSerial) -> int:
    """Characters that break the format: a start bit not 0, a stop bit not 1, a wrong parity."""
    bad = 0
    for bits in chars:
        data = bits[1:1 + p.data_bits]
        ok = bits[0] == 0 and all(bits[1 + p.data_bits + (p.parity is not None):])
        if p.parity:
            ok &= (sum(data) + bits[1 + p.data_bits] + (p.parity == "odd")) % 2 == 0
        bad += not ok
    return bad


def infer_async(cap: Capture) -> AsyncSerial:
    """The asynchronous format that explains every character of the recording, or ValueError.

    The bit time comes from the pulse widths. Every format, at either idle level, is tried on
    every character: one with a framing or parity error is out (a stop bit too many lands on
    the next start bit where characters are back to back). Of the rest, the one with fewer data
    bits that never change wins (more likely stop bits), then fewer data bits (a parity bit
    explains a ninth bit better than data), then more stop bits (safe to send: the recording
    never had less idle)."""
    bit = bit_time(cap.widths())
    best = None
    for idle, data_bits, parity, stop_bits in itertools.product(
            (0, 1), range(5, 10), (None, "even", "odd"), (1, 2)):
        p = AsyncSerial(bit, idle, data_bits, parity, stop_bits)
        chars = characters(cap, p)
        if not chars or errors(chars, p):
            continue
        constant = sum(len({c[1 + k] for c in chars}) == 1 for k in range(data_bits))
        score = (constant, data_bits, -stop_bits)
        if best is None or score < best[0]:
            best = (score, p)
    if best is None:
        raise ValueError(f"no asynchronous format explains the recording (bit {bit:.2f} clocks)")
    return best[1]


def decode(cap: Capture, p: AsyncSerial) -> list[int]:
    """The data values of the recording's characters."""
    return [sum(b << k for k, b in enumerate(bits[1:1 + p.data_bits])) for bits in characters(cap, p)]


# ---------------------------------------------------------------------------------- generate

def timing(p: AsyncSerial) -> tuple[int, int, int]:
    """(PAU half-bit H, CLKDIV integer, fraction / 256) giving a bit of p.bit clocks: H = 8 when
    a bit lasts 16 clocks or more, fewer strobes a bit for faster lines."""
    h = 8 if p.bit >= 16 else max(1, int(p.bit // 2))
    div = round(p.bit / (2 * h) * 256)
    return h, div >> 8, div & 0xFF


def _header(name: str, p: AsyncSerial, h: int) -> str:
    pau = f".pau code=nrz baud={h}{' inv' if not p.idle else ''}"
    crc = f".crc width=1 poly=1{' init=ones' if p.parity == 'odd' else ''}" if p.parity else ""
    return f".program {name}\n; Generated by sim/learn/learn.py: {p}.\n{pau}\n{crc}\n"


def rx_program(p: AsyncSerial, pin: int) -> str:
    """A receiver through the PAU (NRZ; INV for an idle-low line). RX FIFO: a word a character,
    the data in bits 15:16-n with the parity bit above it; IRQ 0 framing error, IRQ 1 parity
    error. The parity is a 1-bit CRC fed from the ISR (ISA.md, PAU)."""
    h, _, _ = timing(p)
    n = p.data_bits
    parity = f"""    in pins, 1              ; the parity bit
    pau crc_feed {n + 1}
    jmp pau crc_ok, good
    irq 1                   ; parity error
good:
""" if p.parity else ""
    return _header("learned_rx", p, h) + f""".in_pins {pin}

    set pau_ctl, rx         ; the bit clock starts at the first edge
.wrap_target
idle:
    in pins, 1              ; ISR shifts right: the bit is bit 15
    mov x, isr
    jmp x 15, idle          ; 1: idle or a stop bit
    mov isr, null           ; a start bit
{"    pau crc_reset" + chr(10) if p.parity else ""}    in pins, {n}              ; the data
{parity}    push
    in pins, 1              ; the stop bit
    mov x, isr
    jmp x 15, idle
    irq 0                   ; framing error
.wrap
"""


def tx_program(p: AsyncSerial, pin: int) -> str:
    """A transmitter through the PAU: TX FIFO, a value a word (bits n-1:0). The parity bit is
    the 1-bit CRC of the data, appended (ISA.md, PAU: CRC append)."""
    h, _, _ = timing(p)
    n = p.data_bits
    data = (f"""    pau crc_reset
    set pau_ctl, tx|crc
    out pins, {n}              ; the data, LSB first, into the parity
    set pau_ctl, tx
    pau crc_append          ; the parity bit
""" if p.parity else f"""    out pins, {n}              ; the data, LSB first
""")
    return _header("learned_tx", p, h) + f""".out_pins {pin}
.set_pins {pin} 1

    set pins, {p.idle}              ; the line at rest
    set pindirs, 1
    set pau_ctl, tx         ; the encoder starts from the pin
.wrap_target
    pull block
    mov y, osr
    mov osr, null
    out pins, 1             ; the start bit
    mov osr, y
{data}    xnor osr, osr
    out pins, {p.stop_bits}              ; the stop bits
.wrap
"""


def word_value(p: AsyncSerial, word: int) -> int:
    """The data value of a word of rx_program()."""
    return word >> (16 - p.data_bits - (p.parity is not None)) & ((1 << p.data_bits) - 1)


# ---------------------------------------------------------------------- self-clocked frames
#
# A frame is a burst of edges between idle times. Every PAU line code is tried on the bursts at
# both polarities, as the PAU decodes it (ISA.md, Line code): a code that leaves a bit of a
# burst unexplained before its last 4 bits (an end mark) is out. Of the rest, the structure decides: a CRC
# every frame ends with, then a transition code (a level code explains any line), stuffing,
# the frame's first edge at a bit boundary, no inversion, the simpler code.

CODES = ["nrz", "nrzi", "manchester", "bmc", "fm0", "diff_manchester", "miller"]
LEVEL_CODES = ("nrz", "nrzi")
EVIDENCE = 40       # bits: a CRC of width w is believed from 40 / w + 1 frames, as a chance
                    # match of every frame is about 2^-(w (frames - 1)) per hypothesis tried


@dataclass
class Crc:
    """A CRC as the PAU computes it: over the bits on the wire (after destuffing), MSB first,
    initial value and final XOR all zeros or all ones; from bit `start` of a frame (destuffed)
    to `tail` bits before its end."""
    width: int
    poly: int           # without the x^width term
    init: int           # 1: all ones
    xorout: int
    start: int
    tail: int

    def __str__(self) -> str:
        return (f"CRC-{self.width} 0x{self.poly:X}, init {'ones' if self.init else 0}, xorout "
                f"{'ones' if self.xorout else 0}, from bit {self.start}, {self.tail} bits before "
                f"the end")


@dataclass
class Frames:
    half: float                     # clocks a half-bit
    code: str
    inv: int
    align: int                      # 1: a frame's first edge is mid-bit
    stuff: tuple[int, int] | None   # (STUFF_MODE, STUFF_LEN)
    sync: list[int]                 # the bits every frame starts with
    crc: Crc | None
    frames: list[list[int]]         # the decoded bits, destuffed
    received: list[list[int]] | None = None     # the bits a receiver takes (_received)

    def __str__(self) -> str:
        stuff = (f"stuffing {('ones', 'equal')[self.stuff[0]]} {self.stuff[1]}" if self.stuff
                 else "no stuffing")
        sync = "".join(map(str, self.sync)) or "-"
        return (f"{self.code}{' inverted' if self.inv else ''}"
                f"{', first edge mid-bit' if self.align else ''}, {self.half:.2f} clocks a half-bit, "
                f"{stuff}, sync {sync}, {self.crc or 'no CRC'}")


def act(code: int, half: int, b: int, p: int, level: int) -> int:
    """The line after the action of PAU_CODE `code` for this half, bit b, previous bit p."""
    return (level, level ^ 1, 0, 1)[code >> 2 * (4 * half + 2 * b + p) & 3]


def bursts(cap: Capture, gap: float) -> list[tuple[int, list[int]]]:
    """(the level before the first edge, the edges) of every burst: edges closer than `gap`."""
    out, cur, level = [], [], cap.level
    for t in cap.edges:
        if cur and t - cur[-1] > gap:
            out.append((level, cur))
            level, cur = cap.level_at(cur[-1]), []
        cur.append(t)
    return out + [(level, cur)] if cur else out


def halves(level: int, edges: list[int], h: float, align: int,
           tail: int = 2) -> list[int] | None:
    """The line in each half-bit of a burst from its first edge (with align from the half
    before it), `tail` halves from its last; None when an edge is off the half-bit grid."""
    out = [level] if align else []
    for a, b in zip(edges, edges[1:]):
        n = (b - a) / h
        if round(n) < 1 or abs(n - round(n)) > 0.3:
            return None
        out += [level ^ 1] * round(n)
        level ^= 1
    return out + [level ^ 1] * tail


def decode_halves(hs: list[int], code: int, level: int) -> tuple[list[int], int]:
    """The bits of the halves (ISA.md, Receiver: the b whose actions explain them, 1 when both
    do), and the halves taken before the first that no bit explains."""
    bits, p = [], 0
    for i in range(0, len(hs) - 1, 2):
        fit = [b for b in (1, 0)
               if act(code, 0, b, p, level) == hs[i] and act(code, 1, b, p, hs[i]) == hs[i + 1]]
        if not fit:
            return bits, i
        bits.append(fit[0])
        level, p = hs[i + 1], fit[0]
    return bits, len(hs)


def destuff(bits: list[int], mode: int, n: int) -> tuple[list[int], list[tuple[int, bool]]]:
    """(the kept bits, (index, as the rule wants) of every stuffed bit): mode 0 after n 1s a 0,
    mode 1 after n equal bits their complement. A wrong one is kept; either way it restarts the
    count (ISA.md, Receiver)."""
    out, marks, run, last = [], [], 0, None
    for i, b in enumerate(bits):
        if run == n:
            ok = b == (0 if mode == 0 else 1 - last)
            marks.append((i, ok))
            out += [] if ok else [b]
            run, last = mode, b                          # mode 1: it starts the next run
            continue
        out.append(b)
        run = (run + 1 if b else 0) if mode == 0 else (run + 1 if b == last else 1)
        last = b
    return out, marks


def common_prefix(seqs: list[list[int]]) -> int:
    k = 0
    while all(len(s) > k for s in seqs) and len({s[k] for s in seqs}) == 1:
        k += 1
    return k


def common_suffix(seqs: list[list[int]]) -> int:
    return common_prefix([s[::-1] for s in seqs])


# Polynomials over GF(2) as ints, bit k the coefficient of x^k.

def deg(p: int) -> int:
    return p.bit_length() - 1


def pmod(a: int, m: int) -> int:
    while deg(a) >= deg(m):
        a ^= m << deg(a) - deg(m)
    return a


def pdiv(a: int, m: int) -> int:
    q = 0
    while deg(a) >= deg(m):
        q |= 1 << deg(a) - deg(m)
        a ^= m << deg(a) - deg(m)
    return q


def pgcd(polys) -> int:
    g = 0
    for a in polys:
        while a:
            g, a = a, pmod(g, a)
    return g


def factor(h: int, w: int) -> int | None:
    """A factor of h of degree w with a constant term, as a CRC polynomial has: h is that
    polynomial times a power of x, or times a small factor (up to 8 degrees) every recorded
    frame shares by chance."""
    if h == 0:
        return None
    h >>= deg(h & -h)
    k = deg(h) - w
    if not 0 <= k <= 8:
        return None
    for q in [1] if k == 0 else range(1 << k | 1, 2 << k, 2):
        if pmod(h, q) == 0 and pdiv(h, q) & 1:
            return pdiv(h, q)
    return None


def to_int(bits: list[int]) -> int:
    return int("".join(map(str, bits)) or "0", 2)


def find_crc(frames: list[list[int]], starts, tails) -> Crc | None:
    """The widest CRC every frame ends with, `tail` bits before its end, over its bits from
    `start`.

    A frame's bits from `start` to the end of its CRC, as a polynomial B of length L, make
    B + I x^(L - w) + X a multiple of the polynomial P (I, X: the initial value and the final
    XOR, 0 or w ones). Frames aligned at their ends differ by multiples of P when I = 0, at
    their starts when X = 0: the gcd of the differences is P, whatever the width. With I and X
    both all ones, every width is tried on the frames themselves."""
    best = None
    for tail in tails:
        for s in starts:
            bs = [(to_int(f[s:len(f) - tail]), len(f) - tail - s) for f in frames]
            n = max(k for _, k in bs)
            if min(k for _, k in bs) < 2:
                continue
            by_end = pgcd(v ^ bs[0][0] for v, _ in bs[1:])
            by_start = pgcd(v << n - k ^ bs[0][0] << n - bs[0][1] for v, k in bs[1:])
            for w in range(32, best.width if best else 0, -1):
                ones = (1 << w) - 1
                for init, xo in ((0, 0), (0, 1), (1, 0), (1, 1)):
                    ds = [v ^ (ones << k - w if init and k > w else 0) ^ (ones if xo else 0)
                          for v, k in bs]
                    p = factor(by_start if init else by_end, w) if not (init and xo) else None
                    if p is None and init and xo:
                        g = 0
                        for d in ds:                     # the gcd, stopped once below w
                            g = pgcd([g, d])
                            if deg(g) < w:
                                break
                        p = factor(g, w) if deg(g) >= w else None
                    if p and all(pmod(d, p) == 0 for d in ds):
                        best = Crc(w, p ^ 1 << w, init, xo, s, tail)
                        break
                if best and best.width == w:
                    break
    return best


def infer_frames(cap: Capture, gap: float = 10) -> Frames:
    """The self-clocked framing that explains every burst of the recording, or ValueError.
    Bursts are apart by more than `gap` times the narrowest pulse."""
    widths = cap.widths()
    if not widths:
        raise ValueError("no edges: nothing to learn")
    shortest = sorted(widths)[:max(1, len(widths) // 10)]
    unit = sum(shortest) / len(shortest)                 # a half-bit, or a bit of a level code
    bs = bursts(cap, gap * unit)
    inside = [b - a for _, e in bs for a, b in zip(e, e[1:])]
    found = []
    for h in (unit, unit / 2):
        for _ in range(3):                               # least squares over the half-bit counts
            counted = [(w, round(w / h)) for w in inside if 0 < round(w / h) <= 32]
            h = sum(w for w, _ in counted) / sum(n for _, n in counted)
        for name in CODES:
            if name in LEVEL_CODES and h > unit * 0.75:  # a level code's narrowest pulse is a bit
                continue
            for inv, align in itertools.product((0, 1), (0,) if name in LEVEL_CODES else (0, 1)):
                raw = []
                for level, edges in bs:
                    hs = halves(level ^ inv, edges, h, align)
                    bits, used = decode_halves(hs, PAU_CODES[name], level ^ inv) if hs else ([], 0)
                    if not hs or used < len(hs) - 8:     # unexplained before the last 4 bits (an
                        break                            # end mark: USB SE0, 10BASE-T TP_IDL)
                    raw.append(bits)
                else:
                    found += _structure(h, name, inv, align, raw)
    if not found:
        raise ValueError("no line code explains every burst of the recording")
    p = max(found, key=lambda f: f[0])[1]
    p.received = _received(p, bs)
    return p


def frames_crc(frames: list[list[int]]) -> Crc | None:
    """The CRC the frames end with, from up to 16 bits before the bits they all start with to
    24 after, ending up to 3 bits before the bits they all end with; believed from EVIDENCE."""
    sync, end = common_prefix(frames), common_suffix(frames)
    crc = find_crc(frames, range(max(0, sync - 16), sync + 25), range(end + 4))
    return crc if crc and crc.width * (len(frames) - 1) >= EVIDENCE else None


def _structure(h: float, name: str, inv: int, align: int, raw: list[list[int]]) -> list[tuple]:
    """(score, Frames) for each stuffing rule no recorded bit breaks (and none), with the CRC
    its frames end with. Stuffing stops at the bits every frame ends with (a delimiter, an end
    flag); a rule needs the CRC or 8 stuffed bits to be believed."""
    pre, suf = common_prefix(raw), common_suffix(raw)
    rules = [None]
    for mode, n in itertools.product((0, 1), range(1, 16)):
        marks = [ok for f in raw for i, ok in destuff(f[:len(f) - suf], mode, n)[1] if i >= pre]
        if marks and all(marks):
            rules.append((mode, n, len(marks)))
    out = []
    for rule in rules:
        frames = ([destuff(f[:len(f) - suf], *rule[:2])[0] + f[len(f) - suf:] for f in raw]
                  if rule else raw)
        sync, crc = common_prefix(frames), frames_crc(frames)
        if rule and not crc and rule[2] < 8:
            continue
        score = (crc.width if crc else 0, name not in LEVEL_CODES, rule is not None, -align, -inv,
                 -CODES.index(name))
        out.append((score, Frames(h, name, inv, align, rule and rule[:2], frames[0][:sync], crc,
                                  frames)))
    return out


def crc_steps(bits: list[int], c: Crc):
    """The CRC after each of these bits, from its initial value (the PAU's, right-aligned)."""
    mask = (1 << c.width) - 1
    reg = mask if c.init else 0
    for b in bits:
        reg = (reg << 1 & mask) ^ (c.poly if (reg >> c.width - 1 & 1) ^ b else 0)
        yield reg


def _received(p: Frames, bs: list[tuple[int, list[int]]]) -> list[list[int]] | None:
    """The bits a receiver takes of every burst, destuffed, to the end it sees: with a
    transition code the decoder's last bit (its run, then RX idle); with a level code the first
    stuff error, where the idle line or an end mark breaks the rule. None when a frame's end
    does not show (a level code without stuffing, or idle that keeps the rule)."""
    level = p.code in LEVEL_CODES
    if level and not p.stuff:
        return None
    run = PAU_CODE_RX.get(p.code, ("edge", 2))[1]
    out = []
    for lv, edges in bs:
        hs = halves(lv ^ p.inv, edges, p.half, p.align, 2 * p.stuff[1] + 4 if level else run)
        bits = decode_halves(hs, PAU_CODES[p.code], lv ^ p.inv)[0]
        if level:
            bad = [i for i, ok in destuff(bits, *p.stuff)[1] if not ok]
            if not bad:
                return None
            bits = bits[:bad[0] + 1]
        out.append(destuff(bits, *p.stuff)[0] if p.stuff else bits)
    return out


def crc_check(p: Frames) -> tuple[int, int] | None:
    """(CRC_RES, K): the CRC right after a correct frame's CRC, and the bits a receiver takes
    after the last bit with that CRC (the CRC's last, or a later one that keeps it: a residue 0
    and 0 bits) to the frame's end, both the same for every recorded frame. None when there is
    no such pair (or K > 30)."""
    if not p.crc or not p.received:
        return None
    pairs = set()
    for f, r in zip(p.frames, p.received):
        end = len(f) - p.crc.tail
        if r[:end] != f[:end]:
            return None
        crcs = list(crc_steps(r[p.crc.start:], p.crc))
        res = crcs[end - p.crc.start - 1]
        last = max(i for i, v in enumerate(crcs) if v == res) + p.crc.start + 1
        pairs.add((res, len(r) - last))
    return pairs.pop() if len(pairs) == 1 and next(iter(pairs))[1] <= 30 else None


def frame_timing(p: Frames) -> tuple[int, int, int]:
    """(PAU half-bit H, CLKDIV integer, fraction / 256): H = 4 strobes a half-bit when it lasts
    4 clocks or more, fewer for faster lines."""
    h = max(1, min(4, int(p.half)))
    div = round(p.half / h * 256)
    return h, div >> 8, div & 0xFF


def pau_directives(p: Frames) -> tuple[str, int, int]:
    """(the .pau and .crc lines, CLKDIV integer, fraction / 256); res= when a receiver can
    check the CRC (crc_check)."""
    h, div, frac = frame_timing(p)
    check = crc_check(p)
    pau = (f".pau code={p.code} baud={h}{' inv' if p.inv else ''}"
           f"{' align=mid' if p.align and p.code not in ('manchester', 'diff_manchester') else ''}"
           f"{' align=edge' if not p.align and p.code in ('manchester', 'diff_manchester') else ''}"
           + (f" stuff={p.stuff[1]}{' stuff_mode=equal' if p.stuff[0] else ''}" if p.stuff else ""))
    crc = (f"\n.crc width={p.crc.width} poly=0x{p.crc.poly:X}{' init=ones' if p.crc.init else ''}"
           f"{' xorout=1' if p.crc.xorout else ''}"
           f"{f' res=0x{check[0]:X}' if check else ''}") if p.crc else ""
    return pau + crc, div, frac


def _asm(lines: list[tuple[str, str]]) -> str:
    """Program lines from (instruction or label:, comment)."""
    return "".join((i if i.endswith(":") else f"    {i:<23} {'; ' * bool(c)}{c}".rstrip()) + "\n"
                   for i, c in lines)


def frames_rx_program(p: Frames, pin: int) -> str:
    """A receiver of the frames: the PAU decodes, destuffs and computes the CRC; the program
    passes every bit on and marks each frame's end with the CRC's verdict. RX FIFO: a frame's
    bits, 8 a word in bits 15:8 (the first in bit 8); then an end word: the last 0..7 bits in
    bits 15:8 under a 1 (the highest 1), bits 7:0 = 1, or 3 when the CRC is right.

    A frame's first edge starts the bits (RX on). It ends at RX idle with a transition code
    (checked again H / 2 later: Miller holds a level 2 bits, and an edge may come a little
    late), at a stuff error with a level code (the idle line or an end mark breaks the rule).
    The CRC starts at the learned bit; Y counts down the bits since CRC == CRC_RES last held,
    from K + 1 (crc_check): the CRC is right when Y is 0 at the end. A bit takes the program
    up to 5 strobes: H >= 3."""
    h = frame_timing(p)[0]
    if h < 3:
        raise ValueError("a half-bit under 3 clocks: too fast for the program")
    if p.received is None:
        raise ValueError("no frame end a receiver sees: a level code needs a stuff error after it")
    check = crc_check(p)
    rx = "rx|stuff" if p.stuff else "rx"
    start = p.crc.start if check else 0
    code = [("frame:", ""), (f"set pau_ctl, {rx}", "the frame's first edge starts the bits")]
    if start >= 16:
        code += [(f"set x, {start // 8 - 1}", ""), ("head:", ""),
                 ("in pins, 8 auto", "the bits before the CRC, a byte at a time"),
                 ("jmp x--, head", "")]
    else:
        code += [("in pins, 8 auto", "")] * (start // 8)
    if start % 8:
        code.append((f"in pins, {start % 8} auto", ""))
    if check:
        code += [("pau crc_reset", ""), (f"set pau_ctl, {rx}|crc", ""), ("mov y, null", "")]
    if not start:
        code.append(("in pins, 1 auto", "the first bit"))
    code += [("jmp bit", ""), ("take:", ""), ("in pins, 1 auto", "")]
    if check:
        code += [("jmp !pau crc_ok, older", ""),
                 (f"set y, {check[1] + 1}", f"CRC == CRC_RES: {check[1]} bits to the end")]
    code += [("older:", ""), ("jmp y--, bit", "Y - 1 either way")] if check else []
    code += [("bit:", ""), ("jmp pau rx_ready, take", "")]
    if p.code in LEVEL_CODES:
        code += [("jmp !pau stuff_err, bit", "the frame ends at a stuff error"),
                 ("jmp pau rx_ready, take", "its bit, if it came with the check")]
    else:
        code += [("jmp !pau rx_idle, bit", "the frame ends at RX idle"),
                 (f"nop [{h // 2}]", ""),
                 ("jmp !pau rx_idle, bit", "an edge a little late: not yet")]
    code += [("set pau_ctl, 0", ""), ("set x, 1", ""), ("in x, 1", "a 1 past the last bits"),
             ("fill:", ""), ("jmp status isr_full, done", ""), ("in null, 1", ""),
             ("jmp fill", ""), ("done:", "")]
    if check:
        code += [("jmp y--, last", "the CRC is wrong"), ("set x, 3", "")]
    code += [("last:", ""), ("or isr, x", "bits 7:0: 1, or 3"), ("push", ""), ("jmp frame", "")]
    return (f".program learned_frames_rx\n; Generated by sim/learn/learn.py: {p}.\n"
            "; RX FIFO: a frame's bits, 8 a word in bits 15:8 (the first in bit 8); then an end\n"
            "; word: the last 0..7 bits in bits 15:8 under a 1, bits 7:0 = 1, or 3 when the CRC\n"
            "; is right.\n"
            f"{pau_directives(p)[0]}\n.in_pins {pin}\n.shift_in right\n.push_thresh 8\n\n"
            + _asm(code))


# ---------------------------------------------------------------------- clocked lines
#
# A clock pin and data pins that change at one of its edges (the shift edge) and hold at the
# other (the sampling edge), as the PAU's external clock takes them (ISA.md, Bit timing): SPI,
# SD card commands, ... Frames are the times a select pin is active, else the clock's bursts.


@dataclass
class Clocked:
    clock: int                          # pins of the recording
    data: int
    select: tuple[int, int] | None      # (pin, active level)
    cpol: int
    cpha: int
    period: float                       # clocks between sampling edges in a frame
    pause: int                          # the longest wait for a bit in a frame (clocks)
    gap: int                            # the shortest from a frame's last bit to the next's first
    half: int                           # the clock's shortest half period
    sync: list[int]
    crc: Crc | None
    frames: list[list[int]]

    def __str__(self) -> str:
        sel = (f", frames while pin {self.select[0]} is {self.select[1]}" if self.select
               else ", frames between clock pauses")
        sync = "".join(map(str, self.sync)) or "-"
        return (f"data pin {self.data} on clock pin {self.clock}, mode {self.cpol * 2 + self.cpha} "
                f"(CPOL {self.cpol}, CPHA {self.cpha}), {self.period:.2f} clocks a bit{sel}, "
                f"sync {sync}, {self.crc or 'no CRC'}")


def infer_clocked(caps: dict[int, Capture], gap: float = 10) -> list[Clocked]:
    """A Clocked for each data pin of a recording of several pins, or ValueError. The clock is
    the pin with the most edges. A select pin is at one level at every clock edge, and a frame
    is the clock's edges while it is; without one, a burst of edges apart by more than `gap`
    times the clock's narrowest pulse. A data pin changes, between a frame's first and last
    clock edges, nearer to one edge of the clock than to the other, always the same."""
    clock = max(caps, key=lambda k: len(caps[k].edges))
    clk = caps[clock]
    widths = clk.widths()
    if len(widths) < 2:
        raise ValueError("no clock: too few edges")
    half = min(widths)
    bs = [(e[0], e[-1]) for _, e in bursts(clk, gap * half) if len(e) > 1]
    edges = [t for t in clk.edges if any(a <= t <= b for a, b in bs)]   # not a lone edge to idle
    select = None
    for k, c in caps.items():
        levels = {c.level_at(t) for t in edges}
        if k != clock and c.edges and len(levels) == 1:
            select = (k, levels.pop())
            break
    if select:
        sel = caps[select[0]]
        bounds = [-1] + sel.edges + [clk.edges[-1] + 1]
        spans = [[t for t in edges if a < t < b] for a, b in zip(bounds, bounds[1:])
                 if sel.level_at(a + 0.5) == select[1]]
        spans = [(ts[0], ts[-1]) for ts in spans if ts]
    else:
        spans = bs
    idle = {clk.level_at(a - 0.5) for a, _ in spans}
    if len(idle) != 1:
        raise ValueError("the clock does not rest at one level between frames")
    cpol = idle.pop()
    inside = lambda t: any(a < t < b for a, b in spans)         # noqa: E731
    out = []
    for k, c in caps.items():
        if k == clock or select and k == select[0] or not any(inside(t) for t in c.edges):
            continue
        near = set()                          # the clock's level after its edge nearest to each
        for t in c.edges:                     # data edge in a burst
            if inside(t):
                i = bisect.bisect_left(clk.edges, t)
                e = min(clk.edges[max(0, i - 1):i + 1], key=lambda x: abs(x - t))
                near.add(clk.level_at(e))
        if len(near) != 1:
            continue                          # not data on this clock
        rising = near.pop() == 0              # data shifts as the clock falls: sampled rising
        cpha = cpol if rising else 1 - cpol
        samples = [t for t in edges if clk.level_at(t) == rising]
        frames, times = [], []
        for a, b in spans:
            ts = [t for t in samples if a <= t <= b]
            if ts:
                frames.append([c.level_at(t) for t in ts])
                times.append(ts)
        steps = [y - x for ts in times for x, y in zip(ts, ts[1:])]
        if len(frames) < 2 or not steps:
            continue
        out.append(Clocked(clock, k, select, cpol, cpha, sorted(steps)[len(steps) // 2],
                           max(steps), min(b[0] - a[-1] for a, b in zip(times, times[1:])), half,
                           frames[0][:common_prefix(frames)], frames_crc(frames), frames))
    if not out:
        raise ValueError("no pin changes with one edge of the clock")
    return out


def clocked_check(p: Clocked) -> int | None:
    """CRC_RES: the CRC after a whole frame, the same for every recorded frame; None if not."""
    if not p.crc:
        return None
    ends = {list(crc_steps(f[p.crc.start:], p.crc))[-1] for f in p.frames}
    return ends.pop() if len(ends) == 1 else None


def clocked_timing(p: Clocked) -> tuple[int, int, int]:
    """(CLKDIV, DLYMUL, TIMEOUT_VAL): every half of the clock seen by 2 strobes, 6 strobes a bit
    for the program, and (without a select pin) a timeout between the longest wait for a bit in
    a frame and the shortest pause between frames."""
    t = (p.pause * p.gap) ** 0.5
    div = max(1, -(-int(t) // 961)) if not p.select else 1
    if div > min(p.half / 2, p.period / 6):
        raise ValueError("the frames' pauses do not fit the clock: no timeout tells them apart")
    if not p.select and p.gap < 2 * p.pause:
        raise ValueError("frames not apart: pauses between them as long as within them")
    ts = round(t / div)
    mul = max(1, -(-ts // 31))
    return div, mul, min(31, round(ts / mul))


def clocked_rx_program(p: Clocked, pin: int, select: int | None = None) -> str:
    """A receiver of the clocked frames through the PAU's external clock: data on `pin`, the
    clock on pin + 1, the select pin (when the recording had one) on `select`. RX FIFO as
    frames_rx_program(): 8 bits a word in bits 15:8, then an end word, 3 when the CRC is right.

    A frame starts as the select pin turns active and ends as it turns inactive; without one,
    the PAU receives all the time and a frame ends when no bit comes for the timeout. The
    program feeds the CRC with each bit it takes (CRC feed), so a frame's first bits may wait
    in the queue while it ends the frame before; it checks the CRC once, at the end (the bits
    after the CRC are the same in every frame)."""
    div, mul, val = clocked_timing(p)
    if p.select and select is None:
        raise ValueError("the recording has a select pin: give its pin")
    res = clocked_check(p)
    start = p.crc.start if res is not None else 0
    off = f"{'!' if p.select and p.select[1] else ''}gpio {select}"
    code = []
    if not p.select:
        code += [(f"set dlymul, {mul}", ""),
                 (f"set timeout_val, {val}", f"{mul * val} strobes without a bit: the end"),
                 ("set pau_ctl, rx", "")]
    code += [("frame:", "")]
    if p.select:
        code += [(f"wait 1 {off}", ""), (f"wait 0 {off}", "selected: a frame starts"),
                 ("set pau_ctl, rx", "")]
    code += [("in pins, 8 auto", "")] * (start // 8)
    if start % 8:
        code.append((f"in pins, {start % 8} auto", ""))
    if res is not None:
        code += [("pau crc_reset", "")]
    if p.select:
        code += [("jmp bit", "")]
    code += [("take:", "")]
    if res is not None:
        code += [("in pins, 1", ""), ("pau crc_feed, 1", "the CRC takes the bit"),
                 ("push iffull", "")]
    else:
        code += [("in pins, 1 auto", "")]
    code += [("bit:", "")]
    if p.select:
        code += [("jmp pau rx_ready, take", ""), (f"jmp {off}, ended", "deselected"),
                 ("jmp bit", ""), ("ended:", ""),
                 ("jmp pau rx_ready, take", "a last bit, if it came with the check"),
                 ("set pau_ctl, 0", "")]
    else:
        code += [("wait 1 pau rx_ready timeout", "a bit, or the frame's end"),
                 ("jmp !status timeout, take", "")]
    code += [("set x, 1", ""), ("in x, 1", "a 1 past the last bits"),
             ("fill:", ""), ("jmp status isr_full, done", ""), ("in null, 1", ""),
             ("jmp fill", ""), ("done:", "")]
    if res is not None:
        code += [("jmp !pau crc_ok, last", ""), ("set x, 3", "the CRC is right")]
    code += [("last:", ""), ("or isr, x", "bits 7:0: 1, or 3"), ("push", ""), ("jmp frame", "")]
    crc = (f".crc width={p.crc.width} poly=0x{p.crc.poly:X}{' init=ones' if p.crc.init else ''}"
           f" res=0x{res:X}\n") if res is not None else ""
    return (f".program learned_clocked_rx\n; Generated by sim/learn/learn.py: {p}.\n"
            f"; Pins: data {pin}, clock {pin + 1}"
            + (f", select {select} ({'high' if p.select[1] else 'low'} in a frame)" if p.select
               else "") + f". CLKDIV {div}.\n"
            "; RX FIFO: a frame's bits, 8 a word in bits 15:8 (the first in bit 8); then an end\n"
            "; word: the last 0..7 bits in bits 15:8 under a 1, bits 7:0 = 1, or 3 when the CRC\n"
            "; is right.\n"
            f".pau code=nrz ext{' cpol=1' if p.cpol else ''}{' cpha=1' if p.cpha else ''}\n{crc}"
            f".in_pins {pin}\n.shift_in right\n.push_thresh 8\n\n" + _asm(code))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("capture", help='JSON: {"clock_hz": .., "level": 0|1, "edges": [clock, ..]}')
    ap.add_argument("--pin", type=int, default=0,
                    help="the generated programs' pin (--clocked: data, the clock on pin + 1)")
    ap.add_argument("--frames", action="store_true", help="self-clocked frames, not async characters")
    ap.add_argument("--clocked", action="store_true", help="a clock pin and data pins")
    ap.add_argument("--select", type=int, help="--clocked: the select pin of the generated program")
    args = ap.parse_args()
    rec = json.load(open(args.capture))
    if args.clocked:
        for p in infer_clocked({int(k): Capture(v["level"], v["edges"])
                                for k, v in rec["pins"].items()}):
            print(p)
            try:
                print(clocked_rx_program(p, args.pin, args.select))
            except ValueError as e:
                print(f"no RX program: {e}")
        return
    cap = Capture(rec["level"], rec["edges"])
    if args.frames:
        p = infer_frames(cap)
        _, div, frac = frame_timing(p)
        print(f"{p}: {rec.get('clock_hz', 50e6) / (2 * p.half):.0f} bit/s, {len(p.frames)} frames; "
              f"CLKDIV {div} + {frac}/256")
        try:
            print(frames_rx_program(p, args.pin))
        except ValueError as e:
            print(f"no RX program: {e}\n{pau_directives(p)[0]}")
        return
    p = infer_async(cap)
    h, div, frac = timing(p)
    print(f"{p}: {rec.get('clock_hz', 50e6) / p.bit:.0f} baud; CLKDIV {div} + {frac}/256")
    print(rx_program(p, args.pin))
    print(tx_program(p, args.pin))


if __name__ == "__main__":
    main()
