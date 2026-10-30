"""learn.py - find a protocol's parameters from a recording of the wire, and write the openPIO
programs that receive and send it.

openPIO's line layer is a small space of numbers (ISA.md, Principles): a protocol of its family
is one point in it. Learning is a search of that space against a recording:

    capture  -> edges of a pin (programs/logic/edges.asm on the chip, or any logic analyzer)
    infer    -> the parameters, each checked against every character of the recording
    generate -> RX and TX programs for those parameters, as assembler source

This first step covers asynchronous serial characters (UART, RS-485, LIN, Modbus RTU, DMX,
MIDI and the proprietary links built on them): bit time, polarity, data bits, parity, stop
bits. Next: synchronous lines with a clock pin, self-clocked frames (line code, stuffing, CRC)
and pulse-width codes.

    python3 sim/learn/learn.py capture.json      # {"clock_hz": .., "level": 0|1, "edges": [t, ..]}
"""
from __future__ import annotations

import argparse
import bisect
import itertools
import json
from dataclasses import dataclass


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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("capture", help='JSON: {"clock_hz": .., "level": 0|1, "edges": [clock, ..]}')
    ap.add_argument("--pin", type=int, default=0, help="the pin of the generated programs")
    args = ap.parse_args()
    rec = json.load(open(args.capture))
    p = infer_async(Capture(rec["level"], rec["edges"]))
    h, div, frac = timing(p)
    print(f"{p}: {rec.get('clock_hz', 50e6) / p.bit:.0f} baud; CLKDIV {div} + {frac}/256")
    print(rx_program(p, args.pin))
    print(tx_program(p, args.pin))


if __name__ == "__main__":
    main()
