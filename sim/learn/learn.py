"""learn.py - find a protocol's parameters from a recording of the wire, and write the openPIO
programs that receive and send it.

openPIO's line layer is a small space of numbers (ISA.md, Principles): a protocol of its family
is one point in it. Learning is a search of that space against a recording:

    capture  -> edges of a pin (programs/logic/edges.asm on the chip, or any logic analyzer:
                Capture.from_sigrok reads PulseView's session files)
    infer    -> the parameters, each checked against every character of the recording
    generate -> RX and TX programs for those parameters, as assembler source

Four families so far:

- asynchronous serial characters (UART, RS-485, LIN, Modbus RTU, DMX, MIDI and the proprietary
  links built on them): bit time, polarity, data bits, parity, stop bits; RX and TX programs.
- self-clocked frames, bursts of edges between idle times (USB, CAN, HDLC, DALI, 10BASE-T,
  MFM, biphase mark, ...): the PAU's line code, polarity, half-bit, bit stuffing, the sync bits
  every frame starts with and the CRC; an RX program that passes the frames on with the CRC's
  verdict.
- clocked lines, a clock pin and data pins (SPI, SD card commands, PS/2 devices, ...): the
  clock, its mode (CPOL, CPHA), the select pin or the pauses that frame the data, sync bits,
  the CRC; an RX program through the PAU's external clock.
- pulse-width codes, a mark and a space a bit (NEC, Sony and air conditioner infrared remotes,
  WS2812, 433 MHz remotes and sensors, ...): the pulse whose width is the bit, its two widths,
  the leader, the CRC; an RX program that times each pulse.

Spikes shorter than a quarter of the narrow pulses (an IR receiver's) are merged first.

    python3 sim/learn/learn.py capture.json   # {"clock_hz": .., "level": 0|1, "edges": [t, ..]}
    python3 sim/learn/learn.py --frames capture.json
    python3 sim/learn/learn.py --clocked pins.json  # {"pins": {"<pin>": {"level", "edges"}, ..}}
    python3 sim/learn/learn.py --pulses capture.json
"""
from __future__ import annotations

import argparse
import bisect
import configparser
import itertools
import json
import re
import sys
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "model"))
from asm import PAU_CODE_RX, PAU_CODES                   # noqa: E402


@dataclass
class Capture:
    """A pin's recording: its level before the first edge, then the clock of every edge; and
    when it is a recording of a running line (a logic analyzer's), its first and last clock:
    a burst within a pause of either may be cut (settled)."""
    level: int
    edges: list[int]
    span: tuple[int, int] | None = None

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

    def settled(self, pause: float) -> tuple[float, float]:
        """The times between which the recording holds no burst it may have cut: after the
        first pause of `pause` clocks or more when its start comes less than that before the
        first edge, before the last such pause when its end comes less than that after the last
        edge. The whole recording without a span (a bench trace starts at power-up)."""
        a, b, e = -1.0, float("inf"), self.edges
        if self.span is None or not e:
            return a, b
        gaps = [k for k in range(len(e) - 1) if e[k + 1] - e[k] >= pause]
        if e[0] - self.span[0] < pause:
            a = e[gaps[0]] if gaps else float("inf")
        if self.span[1] - e[-1] < pause:
            b = e[gaps[-1] + 1] if gaps else -1.0
        return a, b

    def window(self, a: float, b: float) -> "Capture":
        """The edges after clock a and before b."""
        return Capture(self.level_at(a), [t for t in self.edges if a < t < b], self.span)

    def despiked(self) -> "Capture":
        """Without spikes, each merged with the pulses around it: a pulse shorter than a
        quarter of the narrow ones (the tenth narrowest of every ten), as an IR receiver's
        output may break a mark with spikes of a microsecond. Spikes are fewer than one pulse
        in ten, or they are the narrow ones."""
        ws = self.widths()
        if not ws:
            return self
        shortest = sorted(ws)[len(ws) // 10] / 4
        out: list[int] = []
        for t in self.edges:
            if out and t - out[-1] < shortest:
                out.pop()
            else:
                out.append(t)
        return Capture(self.level, out, self.span)

    @staticmethod
    def from_sigrok(path: str | Path, clock_hz: float) -> dict[str, "Capture"]:
        """Every channel of a sigrok session file (.sr, as PulseView saves it), by name, its
        times in clocks of `clock_hz` (the chip's clock, which the generated programs count)."""
        with zipfile.ZipFile(path) as z:
            meta = configparser.ConfigParser(interpolation=None)
            meta.read_string(z.read("metadata").decode())
            dev = meta[next(s for s in meta.sections() if s.startswith("device"))]
            name, unit = dev["capturefile"], int(dev.get("unitsize", "1"))
            parts = sorted((n for n in z.namelist() if n == name or n.startswith(name + "-")),
                           key=lambda n: int(n[len(name) + 1:] or 0))
            data = b"".join(z.read(n) for n in parts)
        num, mult = re.fullmatch(r"\s*([\d.]+)\s*([kMG]?)Hz\s*", dev["samplerate"]).groups()
        rate = float(num) * {"": 1, "k": 1e3, "M": 1e6, "G": 1e9}[mult]
        scale, n = clock_hz / rate, len(data) // unit
        runs = [(m.start() // unit, int.from_bytes(m.group(1), "little"))
                for m in re.finditer(b"(.{%d})\\1*" % unit, data[:n * unit], re.S)]
        out = {}
        for key, label in dev.items():
            if key.startswith("probe") and key[5:].isdigit():
                bit = int(key[5:]) - 1
                level, edges = runs[0][1] >> bit & 1, []
                for (t, v), (_, u) in zip(runs[1:], runs):
                    if (v ^ u) >> bit & 1:
                        edges.append(round(t * scale))
                out[label] = Capture(level, edges, (0, round(n * scale)))
        return out


def write_sigrok(path: str | Path, levels: list[int], samplerate: float,
                 channels: int = 8) -> None:
    """A sigrok session file of a level per sample (bit k: channel Dk), for PulseView and
    Capture.from_sigrok."""
    unit = (channels + 7) // 8
    meta = (f"[global]\nsigrok version=0.5.2\n\n[device 1]\ncapturefile=logic-1\n"
            f"total probes={channels}\nsamplerate={samplerate / 1e6:g} MHz\ntotal analog=0\n"
            + "".join(f"probe{k + 1}=D{k}\n" for k in range(channels)) + f"unitsize={unit}\n")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("version", "2")
        z.writestr("metadata", meta)
        z.writestr("logic-1-1", b"".join(lv.to_bytes(unit, "little") for lv in levels))


def rx_frames(words: list[int]) -> list[tuple[list[int], int]]:
    """(bits, verdict) of each frame in the RX words of a generated frames, clocked or pulses
    receiver: 8 bits a word in bits 15:8 (the first in bit 8), then an end word, the last 0..7
    bits under a 1 in bits 15:8, the verdict in bits 7:0 (1, or 3 when the CRC is right)."""
    out, cur = [], []
    for w in words:
        if w & 0xFF:
            last = w >> 8
            out.append((cur + [last >> i & 1 for i in range(last.bit_length() - 1)], w & 0xFF))
            cur = []
        else:
            cur += [w >> 8 + i & 1 for i in range(8)]
    return out


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


def broken(bits: list[int], p: AsyncSerial) -> bool:
    """A character that breaks the format: a start bit not 0, a stop bit not 1, a wrong parity."""
    data = bits[1:1 + p.data_bits]
    ok = bits[0] == 0 and all(bits[1 + p.data_bits + (p.parity is not None):])
    if p.parity:
        ok &= (sum(data) + bits[1 + p.data_bits] + (p.parity == "odd")) % 2 == 0
    return not ok


def errors(chars: list[list[int]], p: AsyncSerial) -> int:
    """Characters that break the format."""
    return sum(broken(bits, p) for bits in chars)


def infer_async(cap: Capture) -> AsyncSerial:
    """The asynchronous format that explains every character of the recording, or ValueError.

    The bit time comes from the pulse widths. Every format, at either idle level, is tried on
    every character: one with a framing or parity error is out (a stop bit too many lands on
    the next start bit where characters are back to back). Of the rest, the one with fewer data
    bits that never change wins (more likely stop bits), then fewer data bits (a parity bit
    explains a ninth bit better than data), then more stop bits (safe to send: the recording
    never had less idle). Characters a logic analyzer's recording may have cut are left out
    first (a level held longer than a character's 12 bits, 9 data, parity and 2 stop, is idle),
    after spikes (Capture.despiked);
    the whole recording is tried when nothing is left. A recording cut in the middle of
    characters back to back has no such pause: as a receiver does, the first characters may
    break the format until one is read from its start bit (in ASCII text that can take 7), then
    none may; up to 16 are left out, fewer than are left."""
    cap = cap.despiked()
    bit = bit_time(cap.widths())
    cut = cap.span is not None and bool(cap.edges) and cap.edges[0] - cap.span[0] < 13 * bit
    for c, lead in ((cap.window(*cap.settled(13 * bit)), 0), (cap, 0), (cap, 16 if cut else 0)):
        best = None
        for idle, data_bits, parity, stop_bits in itertools.product(
                (0, 1), range(5, 10), (None, "even", "odd"), (1, 2)):
            p = AsyncSerial(bit, idle, data_bits, parity, stop_bits)
            chars = characters(c, p)
            out = max((k + 1 for k in range(min(lead, len(chars))) if broken(chars[k], p)), default=0)
            chars = chars[out:]
            if len(chars) <= out or errors(chars, p):
                continue
            constant = sum(len({x[1 + k] for x in chars}) == 1 for k in range(data_bits))
            score = (constant, data_bits, -stop_bits)
            if best is None or score < best[0]:
                best = (score, p)
        if best is not None:
            return best[1]
    raise ValueError(f"no asynchronous format explains the recording (bit {bit:.2f} clocks)")


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
    spare: int = 0      # 1: one recorded frame does not end with it (corrupted on the line)

    def __str__(self) -> str:
        return (f"CRC-{self.width} 0x{self.poly:X}, init {'ones' if self.init else 0}, xorout "
                f"{'ones' if self.xorout else 0}, from bit {self.start}, {self.tail} bits before "
                f"the end{' (one recorded frame wrong)' if self.spare else ''}")

    @property
    def rank(self) -> tuple[bool, int]:
        """Better first: a CRC every frame ends with, then the wider."""
        return not self.spare, self.width


def agreed(values: list, spare: int):
    """The value all of them have, or all but `spare` (None is no value); None if none."""
    top = Counter(v for v in values if v is not None).most_common(1)
    return top[0][0] if top and top[0][1] >= len(values) - spare else None


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
    unstuffed: int = 0              # bits at a frame's start sent without stuffing (a flag)

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


def pauses(widths: list[int], low: float, high: float) -> list[float]:
    """Pauses shorter than `high` to cut bursts at, from the longest: the middle of each jump of
    the widths by 2 or more, from a width of `low` up."""
    ws = sorted(widths)
    return sorted({(a * b) ** 0.5 for a, b in zip(ws, ws[1:])
                   if b >= 2 * a and a >= low and a * b < high * high}, reverse=True)


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
    n = m.bit_length()
    while (k := a.bit_length() - n) >= 0:
        a ^= m << k
    return a


def pdiv(a: int, m: int) -> int:
    q, n = 0, m.bit_length()
    while (k := a.bit_length() - n) >= 0:
        q |= 1 << k
        a ^= m << k
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


def gcds(polys: list[int], spare: int, low: int | None = None) -> list[int]:
    """The gcd of the polynomials; with a spare, also the gcd of all of them but each one
    (from the gcds of the ones before it and after it). A gcd stops once its degree is `low`
    or less: no factor of a higher degree is left to find; with a spare, none is when both
    halves' gcds stop so, as one half holds every polynomial but the spare one."""
    if spare and low is not None:
        half = len(polys) // 2
        if all(deg(gcds(part, 0, low)[0]) <= low for part in (polys[:half], polys[half:])):
            return []
    if not spare:
        g = 0
        for a in polys:
            g = pgcd([g, a])
            if low is not None and deg(g) <= low:
                break
        return [g]
    pre, suf = [0], [0]
    for a in polys:
        pre.append(pgcd([pre[-1], a]) if deg(pre[-1]) != 0 else 1)
    for a in reversed(polys):
        suf.append(pgcd([suf[-1], a]) if deg(suf[-1]) != 0 else 1)
    suf.reverse()
    return [pre[-1]] + [pgcd([pre[k], suf[k + 1]]) for k in range(len(polys))]


def find_crc(frames: list[list[int]], starts, tails, spare: int = 0) -> Crc | None:
    """The widest CRC every frame ends with, `tail` bits before its end, over its bits from
    `start`; with `spare` 1, every frame but one.

    A frame's bits from `start` to the end of its CRC, as a polynomial B of length L, make
    B + I x^(L - w) + X a multiple of the polynomial P (I, X: the initial value and the final
    XOR, 0 or w ones). Frames aligned at their ends differ by multiples of P when I = 0, at
    their starts when X = 0: the gcd of the differences is P, whatever the width. With I and X
    both all ones, every width is tried on the frames themselves."""
    def differences(vs: list[int]) -> list[int]:
        """The gcds of the differences from the first frame; with a spare, of all but each
        frame (the first: the differences from the second)."""
        out = gcds([v ^ vs[0] for v in vs[1:]], spare)
        return out + [pgcd(v ^ vs[1] for v in vs[2:])] if spare else out

    best = None
    for tail in tails:
        for s in starts:
            bs = [(to_int(f[s:len(f) - tail]), len(f) - tail - s) for f in frames]
            n = max(k for _, k in bs)
            if min(k for _, k in bs) < 2:
                continue
            by_end = differences([v for v, _ in bs])
            by_start = differences([v << n - k for v, k in bs])
            for w in range(32, best.width if best else 0, -1):
                ones = (1 << w) - 1
                for init, xo in ((0, 0), (0, 1), (1, 0), (1, 1)):
                    ds = [v ^ (ones << k - w if init and k > w else 0) ^ (ones if xo else 0)
                          for v, k in bs]
                    hs = gcds(ds, spare, w - 1) if init and xo else by_start if init else by_end
                    for h in hs:
                        p = factor(h, w) if deg(h) >= w else None
                        if p and sum(pmod(d, p) != 0 for d in ds) <= spare:
                            best = Crc(w, p ^ 1 << w, init, xo, s, tail, spare)
                            break
                    if best and best.width == w:
                        break
                if best and best.width == w:
                    break
    return best


def infer_frames(cap: Capture, gap: float = 10) -> Frames:
    """The self-clocked framing that explains every burst of the recording, or ValueError.
    Bursts are apart by more than `gap` times the narrowest pulse; when no line code explains
    them so, by a shorter pause (pauses: from 2 narrowest pulses up), tried from the longest:
    DALI's next forward frame comes 6 half-bits after a backward frame. A burst the recording
    may have cut is left out (Capture.settled). Spikes are merged first (Capture.despiked)."""
    cap = cap.despiked()
    widths = cap.widths()
    if not widths:
        raise ValueError("no edges: nothing to learn")
    shortest = sorted(widths)[:max(1, len(widths) // 10)]
    unit = sum(shortest) / len(shortest)                 # a half-bit, or a bit of a level code
    for cut in [gap * unit] + pauses(widths, 2 * unit, gap * unit):
        bs = bursts(cap.window(*cap.settled(cut)), cut)
        found = _explained(bs, unit)
        if found:
            break
    else:
        raise ValueError("no line code explains every burst of the recording")
    p = max(found, key=lambda f: f[0])[1]
    if not p.crc:                   # none a CRC every frame ends with: maybe all frames but one
        p = max(_explained(bs, unit, 1), key=lambda f: f[0])[1]
    p.received = _received(p, bs)
    return p


def _explained(bs: list[tuple[int, list[int]]], unit: float, spare: int = 0) -> list[tuple]:
    """(score, Frames) of every line code, polarity and alignment that explains every burst
    (_structure, with a CRC every frame but `spare` ends with)."""
    inside = [b - a for _, e in bs for a, b in zip(e, e[1:])]
    found = []
    for h in (unit, unit / 2):
        for _ in range(3):                               # least squares over the half-bit counts
            counted = [(w, round(w / h)) for w in inside if 0 < round(w / h) <= 32]
            if not counted:
                break                                    # no pulse of a few half-bits: no code
            h = sum(w for w, _ in counted) / sum(n for _, n in counted)
        if not counted:
            continue
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
                    found += _structure(h, name, inv, align, raw, spare)
    return found


def frames_crc(frames: list[list[int]], spares=(0, 1)) -> Crc | None:
    """The CRC the frames end with, from up to 16 bits before the bits they all start with to
    24 after, ending up to 3 bits before the bits they all end with; believed from EVIDENCE.
    When none is, a CRC all of them but one end with, believed from the others: a frame the
    line corrupted (a reflection, a key half pressed) does not hide the CRC of the rest.
    `spares`: the frames left out to try, in order. A frame of 32 bits or more that is a
    rotation of another (the same, or a pattern going round a LED ring) is no evidence: a
    polynomial that divides x^n + 1 divides every rotation of a frame of n bits it divides,
    and random frames that long are rotations of each other about once in 2^27. Shorter
    frames are, by chance (an 11-bit PS/2 byte), and each is counted."""
    sync, end = common_prefix(frames), common_suffix(frames)
    kinds = len({rotation_class(f) for f in frames})
    for spare in spares if len(frames) > 2 else (0,):
        crc = find_crc(frames, range(max(0, sync - 16), sync + 25), range(end + 4), spare)
        if crc and crc.width * (kinds - 1 - spare) >= EVIDENCE:
            return crc
    return None


def rotation_class(bits: list[int]) -> tuple[int, ...] | int:
    """The least of the rotations of 32 bits or more: the same for a frame and its rotations;
    a shorter frame is a class of its own."""
    if len(bits) < 32:
        return id(bits)
    s = "".join(map(str, bits))
    return tuple(map(int, min(s[k:] + s[:k] for k in range(len(s)))))


def _structure(h: float, name: str, inv: int, align: int, raw: list[list[int]],
               spare: int = 0) -> list[tuple]:
    """(score, Frames) for each stuffing rule no recorded bit breaks (and none), with the CRC
    its frames end with. Stuffing stops at the bits every frame ends with (a delimiter, an end
    flag); it starts after the bits every frame starts with when they break the rule (an HDLC
    flag, six 1s), else at the first bit. A rule needs the CRC or 8 stuffed bits to be
    believed."""
    pre, suf = common_prefix(raw), common_suffix(raw)
    rules = [None]
    for mode, n in itertools.product((0, 1), range(1, 16)):
        k = pre if any(not ok for f in raw for _, ok in destuff(f[:pre], mode, n)[1]) else 0
        marks = [ok for f in raw for i, ok in destuff(f[k:len(f) - suf], mode, n)[1]
                 if i + k >= pre]
        if marks and all(marks):
            rules.append((mode, n, len(marks), k))
    out = []
    for rule in rules:
        k = rule[3] if rule else 0
        frames = ([f[:k] + destuff(f[k:len(f) - suf], *rule[:2])[0] + f[len(f) - suf:]
                   for f in raw] if rule else raw)
        sync, crc = common_prefix(frames), frames_crc(frames, (spare,))
        if rule and not crc and rule[2] < 8:
            continue
        score = (crc.rank if crc else (False, 0), name not in LEVEL_CODES, rule is not None,
                 -align, -inv, -CODES.index(name))
        out.append((score, Frames(h, name, inv, align, rule and rule[:2], frames[0][:sync], crc,
                                  frames, unstuffed=k)))
    return out


def crc_steps(bits: list[int], c: Crc):
    """The CRC after each of these bits, from its initial value (the PAU's, right-aligned)."""
    mask = (1 << c.width) - 1
    reg = mask if c.init else 0
    for b in bits:
        reg = (reg << 1 & mask) ^ (c.poly if (reg >> c.width - 1 & 1) ^ b else 0)
        yield reg


def _received(p: Frames, bs: list[tuple[int, list[int]]]) -> list[list[int]] | None:
    """The bits a receiver takes of every burst, destuffed after the unstuffed ones, to the end
    it sees: with a transition code the decoder's last bit (its run, then RX idle); with a
    level code the first stuff error, where the idle line or an end mark breaks the rule. None
    when a frame's end does not show (a level code without stuffing, or idle that keeps the
    rule)."""
    level = p.code in LEVEL_CODES
    if level and not p.stuff:
        return None
    run = PAU_CODE_RX.get(p.code, ("edge", 2))[1]
    k = p.unstuffed
    out = []
    for lv, edges in bs:
        hs = halves(lv ^ p.inv, edges, p.half, p.align, 2 * p.stuff[1] + 4 if level else run)
        bits = decode_halves(hs, PAU_CODES[p.code], lv ^ p.inv)[0]
        if not p.stuff:
            out.append(bits)
            continue
        rest = bits[k:]
        if level:
            bad = [i for i, ok in destuff(rest, *p.stuff)[1] if not ok]
            if not bad:
                return None
            rest = rest[:bad[0] + 1]
        out.append(bits[:k] + destuff(rest, *p.stuff)[0])
    return out


def crc_check(p: Frames) -> tuple[int, int] | None:
    """(CRC_RES, K): the CRC right after a correct frame's CRC, and the bits a receiver takes
    after the last bit with that CRC (the CRC's last, or a later one that keeps it: a residue 0
    and 0 bits) to the frame's end, both the same for every recorded frame (but the one the CRC
    spares). None when there is no such pair (or K > 30)."""
    if not p.crc or not p.received:
        return None
    pairs = []
    for f, r in zip(p.frames, p.received):
        end = len(f) - p.crc.tail
        if r[:end] != f[:end]:
            pairs.append(None)
            continue
        crcs = list(crc_steps(r[p.crc.start:], p.crc))
        res = crcs[end - p.crc.start - 1]
        last = max(i for i, v in enumerate(crcs) if v == res) + p.crc.start + 1
        pairs.append((res, len(r) - last))
    pair = agreed(pairs, p.crc.spare)
    return pair if pair and pair[1] <= 30 else None


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


def rx_stays_on(p: Frames, check: tuple[int, int] | None) -> bool:
    """Whether a receiver can leave RX on from frame to frame: a transition code's decoder ends
    a frame and waits for the next first edge by itself (ISA.md, Receiver), so the next frame's
    bits wait in the queue while the program ends the last. Only when nothing else carries over
    from a frame: no stuffing count, no previous bit in the code, a CRC from the first bit."""
    code = PAU_CODES[p.code]
    no_p = all(code >> 4 * j & 3 == code >> 4 * j + 2 & 3 for j in range(4))
    return (p.code not in LEVEL_CODES and not p.stuff and no_p
            and (not check or p.crc.start == 0))


def frames_rx_program(p: Frames, pin: int) -> str:
    """A receiver of the frames: the PAU decodes, destuffs and computes the CRC; the program
    passes every bit on and marks each frame's end with the CRC's verdict. RX FIFO: a frame's
    bits, 8 a word in bits 15:8 (the first in bit 8); then an end word: the last 0..7 bits in
    bits 15:8 under a 1 (the highest 1), bits 7:0 = 1, or 3 when the CRC is right.

    A frame's first edge starts the bits (RX on). It ends at RX idle with a transition code,
    4 half-bits without an edge (checked again H / 2 later when the code holds a level that
    long, as Miller does: an edge may come a little late), at a stuff error with a level code
    (the idle line or an end mark breaks the rule).
    Stuffing starts after the unstuffed bits (an HDLC flag), the CRC at the learned
    bit; Y counts down the bits since CRC == CRC_RES last held, from K + 1 (crc_check): the CRC
    is right when Y is 0 at the end. When RX can stay on (rx_stays_on), the next frame may
    start while the program ends one: DALI answers come 6 half-bits apart. A bit takes the
    program up to 5 strobes: H >= 3."""
    h = frame_timing(p)[0]
    if h < 3:
        raise ValueError("a half-bit under 3 clocks: too fast for the program")
    if p.received is None:
        raise ValueError("no frame end a receiver sees: a level code needs a stuff error after it")
    check = crc_check(p)
    start = p.crc.start if check else 0
    k = p.unstuffed
    stuff, crc = "|stuff" * bool(p.stuff), "|crc" * bool(check)
    code = []
    if rx_stays_on(p, check):
        if check:
            code.append(("pau crc_reset", ""))
        code += [(f"set pau_ctl, rx{crc}", "on for good: the PAU waits for each frame"),
                 ("frame:", "")]
        if check:
            code.append(("mov y, null", ""))
        code.append(("in pins, 1 auto", "the first bit"))
        end = [("pau crc_reset", "the next frame's CRC, from its first bit")] if check else []
    else:
        code += [("frame:", ""), (f"set pau_ctl, rx{'' if k else stuff}",
                                  "the frame's first edge starts the bits")]
        taken = 0
        for pos in sorted({k, start}):
            n = pos - taken
            if n >= 16:
                code += [(f"set x, {n // 8 - 1}", ""), (f"head{pos}:", ""),
                         ("in pins, 8 auto", "a byte at a time"), (f"jmp x--, head{pos}", "")]
            else:
                code += [("in pins, 8 auto", "")] * (n // 8)
            if n % 8:
                code.append((f"in pins, {n % 8} auto", ""))
            taken = pos
            if pos == start and check:
                code.append(("pau crc_reset", ""))
            if pos == k and k or pos == start and check:
                code.append((f"set pau_ctl, rx{stuff * (pos >= k)}{crc * (pos >= start)}",
                             "after the sync: stuffing" if pos == k and k else ""))
            if pos == start and check:
                code.append(("mov y, null", ""))
        if not taken:
            code.append(("in pins, 1 auto", "the first bit"))
        end = [("set pau_ctl, 0", "")]
    code += [("jmp bit", ""), ("take:", ""), ("in pins, 1 auto", "")]
    if check:
        code += [("jmp !pau crc_ok, older", ""),
                 (f"set y, {check[1] + 1}", f"CRC == CRC_RES: {check[1]} bits to the end"),
                 ("older:", ""), ("jmp y--, bit", "Y - 1 either way")]
    code += [("bit:", ""), ("jmp pau rx_ready, take", "")]
    if p.code in LEVEL_CODES:
        code += [("jmp !pau stuff_err, bit", "the frame ends at a stuff error"),
                 ("jmp pau rx_ready, take", "its bit, if it came with the check")]
    else:
        code.append(("jmp !pau rx_idle, bit", "the frame ends at RX idle"))
        if PAU_CODE_RX.get(p.code, ("edge", 2))[1] >= 4:
            code += [(f"nop [{h // 2}]", ""),
                     ("jmp !pau rx_idle, bit", "an edge a little late: not yet")]
    code += end + [("set x, 1", ""), ("in x, 1", "a 1 past the last bits"),
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
# A clock pin and data pins that change at one of its edges (the shift edge), or while it rests
# at one level, and hold at the other edge (the sampling edge), as the PAU's external clock takes
# them (ISA.md, Bit timing): SPI, SD card commands, a PS/2 device, ... Frames are the times a
# select pin is active, else the clock's bursts.


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
    times the clock's narrowest pulse, or when no pin is data on such frames by a shorter pause
    (pauses), tried from the longest: a PS/2 device's bytes come 3 clock periods apart. A data
    pin changes, between a frame's first and last
    clock edges, while the clock is at one level, always the same: the sampling edge ends it.
    A data edge within a quarter of the clock's typical half (the median width: a recording's
    times rounded to the chip's clock make the shortest one short) from a clock edge counts as
    after it (the shift edge, whichever side the recording shows the data on)."""
    clock = max(caps, key=lambda k: len(caps[k].edges))
    widths = caps[clock].widths()
    if len(widths) < 2:
        raise ValueError("no clock: too few edges")
    half = min(widths)
    near_by = sorted(widths)[len(widths) // 2] / 4     # a data edge this near a clock edge
    error = ValueError("no pin changes while the clock is at one level")
    for cut in [gap * half] + pauses(widths, half, gap * half):
        a, b = caps[clock].settled(cut)              # not a frame the recording may have cut
        try:
            out = _clocked({k: c.window(a, b) for k, c in caps.items()}, clock, half, near_by,
                           cut)
        except ValueError as e:
            error = e
            continue
        if out:
            return out
    raise error


def _clocked(caps: dict[int, Capture], clock: int, half: int, near_by: float,
             cut: float) -> list[Clocked]:
    """infer_clocked() with clock bursts apart by more than `cut`: [] when no pin is data."""
    clk = caps[clock]
    bs = [(e[0], e[-1]) for _, e in bursts(clk, cut) if len(e) > 1]
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
        near = set()                          # the clock's level at each data edge in a burst
        for t in c.edges:
            if inside(t):
                i = bisect.bisect_left(clk.edges, t)
                e = min(clk.edges[max(0, i - 1):i + 1], key=lambda x: abs(x - t))
                near.add(clk.level_at(e if abs(e - t) <= near_by else t))
        if len(near) != 1:
            continue                          # not data on this clock
        rising = near.pop() == 0              # data changes while the clock is low: sampled rising
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
    return out


def clocked_check(p: Clocked) -> int | None:
    """CRC_RES: the CRC after a whole frame, the same for every recorded frame (but the one the
    CRC spares); None if not."""
    if not p.crc:
        return None
    return agreed([list(crc_steps(f[p.crc.start:], p.crc))[-1] for f in p.frames], p.crc.spare)


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


# ---------------------------------------------------------------------- pulse-width codes
#
# Frames of pulses, a mark (the active level) then a space (the idle level) a bit, the bit in
# the width of the marks (pulse width: Sony SIRC, WS2812, 433 MHz sensors) or of the spaces
# (pulse distance: NEC; a mark after the last bit closes it), maybe after a leader of pulses of
# their own widths. No PAU line code: the program times each pulse with a timed WAIT.

LEADER_MIN = 0.75   # a leader pulse is taken from 3/4 of its shortest recorded width


@dataclass
class Pulses:
    idle: int                   # the line between frames
    carrier: str                # "mark": pulse width; "space": pulse distance, a stop mark
    short: float                # the carrier's two widths, clocks
    long: float
    one: int                    # 1: a long pulse is a 1; 0: a short one
    leader: list[float]         # the shortest recorded width of each pulse before the bits
    narrowest: float            # the narrowest pulse among the bits
    longest: float              # the longest space among the bits
    gap: float                  # the shortest pause between frames
    ignored: int                # frames of another kind (another leader), not in `frames`
    sync: list[int]
    crc: Crc | None
    frames: list[list[int]]

    def __str__(self) -> str:
        kind = "pulse width" if self.carrier == "mark" else "pulse distance"
        sync = "".join(map(str, self.sync)) or "-"
        lead = " ".join(f"{w:.0f}" for w in self.leader) or "none"
        return (f"{kind}, idle {'high' if self.idle else 'low'}, {self.carrier}s of "
                f"{self.short:.1f} / {self.long:.1f} clocks (a {('short', 'long')[self.one]} one "
                f"is a 1), leader {lead}, sync {sync}, {self.crc or 'no CRC'}"
                + (f"; {self.ignored} frames of another kind" if self.ignored else ""))


def groups(widths, ratio: float = 1.3) -> list[tuple[float, float]]:
    """(narrowest, widest) of each group of widths: sorted, a new group where one is more than
    `ratio` times the one before."""
    out: list[list[float]] = []
    for w in sorted(widths):
        if out and w <= out[-1][1] * ratio:
            out[-1][1] = w
        else:
            out.append([w, w])
    return [(a, b) for a, b in out]


def group_of(gs: list[tuple[float, float]], w: float) -> int:
    return next(k for k, (a, b) in enumerate(gs) if a <= w <= b)


def leaders_within(frames: list[list[float]], gap: float) -> tuple[list[list[float]], float]:
    """Each frame that starts with a mark of the widest marks' width (a leader: under one mark
    in eight) split before each such mark within it after a pause at least LEADER_MIN of its own
    leader's space: a Midea air conditioner remote sends each frame twice, 5 ms apart, a pause
    close to its leader's space, and an NEC remote's repeat code may come within the pause
    taken. The shortest pause between frames, those within with them."""
    marks = groups(w for f in frames for w in f[0::2])
    if len(marks) < 2:
        return frames, gap
    lo, hi = marks[-1]
    lead = lambda w: lo <= w <= hi                                 # noqa: E731
    if 8 * sum(lead(w) for f in frames for w in f[0::2]) > sum(len(f[0::2]) for f in frames):
        return frames, gap
    out = []
    for f in frames:
        led = len(f) > 1 and lead(f[0])
        cut = [0] + [k for k in range(2, len(f), 2)
                     if led and lead(f[k]) and f[k - 1] >= LEADER_MIN * f[1]]
        gap = min([gap] + [f[k - 1] for k in cut[1:]])
        out += [f[a:b - 1] for a, b in zip(cut, cut[1:])] + [f[cut[-1]:]]
    return out, gap


def infer_pulses(cap: Capture) -> Pulses:
    """The pulse-width code that explains the recording, or ValueError.

    Spikes are merged first (Capture.despiked). The idle level is the level of the longest
    pulse; frames are apart by the pauses past a jump of the idle widths by 2 or more. Of each
    such jump, from the longest, the one that explains the most frames, less the frames it
    leaves out, is learned: a 433 MHz remote repeats its frame after a sync space, past which
    the pauses between presses leave no frame of one kind; a pause shorter than the right one
    cuts frames, and their leaders are frames of another kind (_pulses)."""
    ws = cap.widths()
    if len(ws) < 8:
        raise ValueError("too few edges")
    cap = cap.despiked()
    ws = cap.widths()
    lv = [cap.level ^ (k + 1 & 1) for k in range(len(ws))]
    idle = lv[max(range(len(ws)), key=ws.__getitem__)]
    spaces = sorted({w for w, v in zip(ws, lv) if v == idle})
    if len(spaces) < 2:
        raise ValueError("no pauses between frames")
    cuts = sorted(((b, a) for a, b in zip(spaces, spaces[1:]) if b >= 2 * a), reverse=True)
    if not cuts:
        raise ValueError("no pauses between frames: no idle width far past the others")
    found, errors = [], []
    for gap, below in cuts:
        try:
            found.append(_pulses(cap, idle, gap, below))
        except ValueError as e:
            errors.append(e)
    if not found:
        raise errors[0]
    return max(found, key=lambda p: len(p.frames) - p.ignored)    # the longest of the best


def _split(cap: Capture, idle: int, gap: float) -> tuple[list[list[float]], float]:
    """The frames between pauses of `gap` or more, a mark first, each its widths, and split
    before a leader within (leaders_within); the shortest pause between them."""
    ws = cap.widths()
    lv = [cap.level ^ (k + 1 & 1) for k in range(len(ws))]
    frames, cur = [], [] if cap.level == idle else None
    for w, v in zip(ws, lv):
        if v == idle and w >= gap:
            if cur:
                frames.append(cur)
            cur = []
        elif cur is not None:
            cur.append(w)
    if cur and lv[-1] != idle:                               # the line idle after the last edge
        frames.append(cur)
    return leaders_within(frames, gap)


def _pulses(cap: Capture, idle: int, gap: float, below: float) -> Pulses:
    """infer_pulses() with frames apart by pauses of `gap` or more, `below` the widest idle
    width short of it. A leader within a frame starts another (leaders_within). Widths fall in
    groups (a mark's and a space's apart); a group is the bits' when a frame holds two pulses
    of it, and the leader is a frame's pulses before the first such, a mark and a space each.
    The frames of the leader whose frames hold the most pulses after it are the ones learned
    (not a remote's repeat codes, though more of them); of those, a frame with a width among
    its bits that under half of them have is left out (a frame the line corrupted: a mark the
    receiver missed makes a space as long as three). Their marks or spaces have two widths, the
    others one (or the complement: a long mark and a short space, a short mark and a long
    space)."""
    frames, within = _split(cap, idle, gap)
    if cap.span:                    # not a frame the recording cut: idle longer than any space
        if within < gap:            # in a frame is a pause: the pauses between frames are
            below = max((w for f in frames for w in f[1::2]), default=below)   # those within
        cap = cap.window(*cap.settled((below * within) ** 0.5))
        frames, within = _split(cap, idle, gap)
    gap = within
    if len(frames) < 2:
        raise ValueError("fewer than two frames")

    gs = (groups(w for f in frames for w in f[0::2]), groups(w for f in frames for w in f[1::2]))
    cls = [[(k & 1, group_of(gs[k & 1], w)) for k, w in enumerate(f)] for f in frames]
    bit = {c for cs in cls for c in cs if cs.count(c) >= 2}
    kinds: dict[tuple, list[list[float]]] = {}
    for f, cs in zip(frames, cls):
        n = next((k for k, c in enumerate(cs) if c in bit), len(f))
        kinds.setdefault(tuple(cs[:n + (n & 1)]), []).append(f)
    key, main = max(kinds.items(),                      # the most pulses past the leader
                    key=lambda kv: (sum(len(f) - len(kv[0]) for f in kv[1]), len(kv[1])))
    n = len(key)
    main = [f for f in main if len(f) > n]             # not a lone leader
    bgs = (groups(w for f in main for w in f[n::2]), groups(w for f in main for w in f[n + 1::2]))
    of = lambda f: {(k & 1, group_of(bgs[k & 1], w)) for k, w in enumerate(f[n:])}  # noqa: E731
    seen = Counter(c for f in main for c in of(f))
    main = [f for f in main if all(2 * seen[c] >= len(main) for c in of(f))]
    if len(main) < 2:
        raise ValueError("fewer than two frames of one kind with bits after the leader")
    body = [f[n:] for f in main]
    marks = [w for b in body for w in b[0::2]]
    gaps = [w for b in body for w in b[1::2]]
    mg, sg = groups(marks), groups(gaps)
    if len(mg) > 2 or len(sg) > 2:
        raise ValueError("more than two widths of a pulse: not a binary pulse code")
    mid = lambda g: (g[0][1] + g[1][0]) / 2                      # noqa: E731
    pairs = [(b[k], b[k + 1]) for b in body for k in range(0, len(b) - 1, 2)]
    if len(mg) == 2 and (len(sg) == 1 or all((m > mid(mg)) != (s > mid(sg)) for m, s in pairs)):
        carrier, values = "mark", [b[0::2] for b in body]
    elif len(sg) == 2 and len(mg) == 1:
        carrier, values = "space", [b[1::2] for b in body]
    else:
        raise ValueError("neither the marks nor the spaces alone carry the bits")
    g = mg if carrier == "mark" else sg
    t = mid(g)
    short = [w for v in values for w in v if w < t]
    long_ = [w for v in values for w in v if w > t]
    raw = [[int(w > t) for w in v] for v in values]
    best = None
    for one in (1, 0):                                   # the sense the CRC is found in
        fs = [[b ^ 1 - one for b in f] for f in raw]
        crc = frames_crc(fs)
        if best is None or crc and (not best[1] or crc.rank > best[1].rank):
            best = (one, crc, fs)
    one, crc, fs = best
    return Pulses(idle, carrier, sum(short) / len(short), sum(long_) / len(long_), one,
                  [min(f[k] for f in main) for k in range(n)], min(marks + gaps),
                  max(gaps, default=0), gap, len(frames) - len(main), fs[0][:common_prefix(fs)],
                  crc, fs)


def pulses_check(p: Pulses) -> int | None:
    """CRC_RES: the CRC after a whole frame, every bit fed from the first (the bits before the
    CRC's start feed it too: the same in every frame, or frames of one length), the same for
    every recorded frame (but the one the CRC spares); None if not."""
    if not p.crc:
        return None
    return agreed([list(crc_steps(f, p.crc))[-1] for f in p.frames], p.crc.spare)


def pulses_timing(p: Pulses) -> tuple[int, list[tuple[int, int]], tuple[int, int], int]:
    """(CLKDIV, (DLYMUL, TIMEOUT_VAL) of each leader pulse's least width, (DLYMUL, TIMEOUT_VAL)
    of the threshold T between the carrier's widths, N): N times T without a mark ends a frame,
    between the longest space among the bits and the shortest pause between frames (their
    geometric mean, at most 32 T). A timed WAIT lasts up to 31 x 31 strobes."""
    t = (p.short + p.long) / 2
    lo = [w * LEADER_MIN for w in p.leader]
    div = max(1, -(-int(max([t] + lo)) // 961))
    if min(p.narrowest, t - p.short, p.long - t) / div < 4:
        raise ValueError("pulses, or their widths' difference, under 4 strobes: too fast")

    def wait(clocks: float) -> tuple[int, int]:
        s = clocks / div
        m = -(-int(s) // 31)
        return m, max(1, int(s / m))

    m, v = wait(t)
    unit = m * v * div
    n = min(-(-int((p.longest * p.gap) ** 0.5) // unit), 32)
    if not p.longest * 1.1 < n * unit < p.gap / 1.1:
        raise ValueError("no timeout tells the pauses between frames from the spaces in them")
    return div, [wait(w) for w in lo], (m, v), n


def pulses_rx_program(p: Pulses, pin: int) -> str:
    """A receiver of the frames on `pin`. RX FIFO as frames_rx_program(): 8 bits a word in bits
    15:8, then an end word, 3 when the CRC is right.

    A frame starts with a mark; each leader pulse must last its least width (else the program
    waits for the next frame), then each carrier pulse is timed by a WAIT with the timeout T
    between its widths: a timeout, a long one. N times T without a mark ends the frame; without
    a leader the program first waits for such a pause, not to start in a frame. The program
    feeds the CRC with each bit and checks it at the end (pulses_check)."""
    div, leader, (m, v), n = pulses_timing(p)
    res = pulses_check(p)
    a, i = 1 - p.idle, p.idle
    w = lambda lv, t="": f"wait {lv} in_base 0{t and ' timeout'}"         # noqa: E731
    long_in, short_in = ("in x, 1", "in null, 1") if p.one else ("in null, 1", "in x, 1")
    auto = "" if res is not None else " auto"
    timing = [(f"set dlymul, {m}", ""),
              (f"set timeout_val, {v}", f"T = {m * v} strobes: a long pulse lasts past it")]
    k = n - 1 - (p.carrier == "space")                  # Y + 1 timeouts after the first
    code = [("set x, 1", "X = 1: a 1 bit")]
    code += [("pau crc_reset", "")] if res is not None else []
    if not p.leader:
        code += timing + [("sync:", ""), (w(i), ""), (f"set y, {n - 1}", ""), ("quiet:", ""),
                          (w(a, "t"), ""), ("jmp !status timeout, sync", "a mark: not a pause"),
                          ("jmp y--, quiet", f"{n} T without a mark: between frames")]
    code += [("frame:", ""), (w(i), ""), (w(a), "a mark: a frame starts")]
    for j, (lm, lv) in enumerate(leader):
        level = (a, i)[j & 1]
        if j == 0 or lm != leader[j - 1][0]:
            code.append((f"set dlymul, {lm}", ""))
        code += [(f"set timeout_val, {lv}", ""),
                 (w(1 - level, "t"), f"leader {('mark', 'space')[j & 1]}: {lm * lv} strobes"),
                 ("jmp !status timeout, frame", "shorter: not a frame")]
        code += timing if j == len(leader) - 1 else []
        code.append((w(1 - level), ""))
    feed = ([("pau crc_feed, 1", "the CRC takes the bit"), ("push iffull", "")]
            if res is not None else [])
    if p.carrier == "mark":
        code += [("bit:", ""), (w(i, "t"), "the mark ends, or lasts past T"),
                 ("jmp !status timeout, short", ""), (w(i), "a long mark: its end"),
                 (long_in + auto, ""), ("jmp space", ""), ("short:", ""), (short_in + auto, ""),
                 ("space:", "")] + feed + [
                 (f"set y, {k}", ""), ("gap:", ""), (w(a, "t"), "the next mark"),
                 ("jmp !status timeout, bit", ""), ("jmp y--, gap", "")]
    else:
        code += [("bit:", ""), (w(i), "the mark ends: a space"), (f"set y, {k}", ""),
                 (w(a, "t"), "the next mark, or the space lasts past T"),
                 ("jmp !status timeout, short", ""), ("gap:", ""), (w(a, "t"), ""),
                 ("jmp !status timeout, long", "a long space"), ("jmp y--, gap", ""),
                 ("jmp end", "the mark before was the stop mark"),
                 ("long:", ""), (long_in + auto, ""), ("jmp next", ""), ("short:", ""),
                 (short_in + auto, ""), ("next:", "")] + feed + [("jmp bit", "")]
    code += [("end:", ""), ("in x, 1", f"{n} T without a mark: a 1 past the last bits"),
             ("fill:", ""), ("jmp status isr_full, done", ""), ("in null, 1", ""),
             ("jmp fill", ""), ("done:", "")]
    if res is not None:
        code += [("jmp !pau crc_ok, last", ""), ("set x, 3", "the CRC is right")]
    code += [("last:", ""), ("or isr, x", f"bits 7:0: 1{', or 3' * (res is not None)}"),
             ("push", "")]
    if res is not None:
        code += [("set x, 1", ""), ("pau crc_reset", "")]
    code += [("jmp frame", "")]
    crc = (f".crc width={p.crc.width} poly=0x{p.crc.poly:X}{' init=ones' if p.crc.init else ''}"
           f" res=0x{res:X}\n") if res is not None else ""
    return (f".program learned_pulses_rx\n; Generated by sim/learn/learn.py: {p}.\n"
            f"; CLKDIV {div}.\n"
            "; RX FIFO: a frame's bits, 8 a word in bits 15:8 (the first in bit 8); then an end\n"
            "; word: the last 0..7 bits in bits 15:8 under a 1, bits 7:0 = 1, or 3 when the CRC\n"
            "; is right.\n"
            f"{crc}.in_pins {pin}\n.shift_in right\n.push_thresh 8\n\n" + _asm(code))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("capture", help='JSON: {"clock_hz": .., "level": 0|1, "edges": [clock, ..]}')
    ap.add_argument("--pin", type=int, default=0,
                    help="the generated programs' pin (--clocked: data, the clock on pin + 1)")
    ap.add_argument("--frames", action="store_true", help="self-clocked frames, not async characters")
    ap.add_argument("--clocked", action="store_true", help="a clock pin and data pins")
    ap.add_argument("--select", type=int, help="--clocked: the select pin of the generated program")
    ap.add_argument("--pulses", action="store_true", help="a pulse-width code")
    args = ap.parse_args()
    rec = json.load(open(args.capture))
    if args.pulses:
        p = infer_pulses(Capture(rec["level"], rec["edges"]))
        print(f"{p}: {len(p.frames)} frames")
        try:
            print(pulses_rx_program(p, args.pin))
        except ValueError as e:
            print(f"no RX program: {e}")
        return
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
