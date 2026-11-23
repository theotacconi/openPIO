#!/usr/bin/env python3
"""listen.py - From a recording to a receiver on the board: sim/learn/learn.py finds the line's
format in a logic analyzer's recording (a sigrok session file, as PulseView saves it), writes
an openPIO receiver for it, and this loads it on state machine 0 and prints what it receives.

    python3 board/listen.py remote.sr --channel D0 --pin 0          # the Pico on /dev/ttyACM0
    python3 board/listen.py bus.sr --channel D2 --pin 3 --kind frames
    python3 board/listen.py lin.sr --channel D0 --kind async        # LIN, DMX: with breaks
    python3 board/listen.py spi.sr --channel D1 --clock-channel D0 --pin 2   # clock on pin 3
    python3 board/listen.py remote.sr --channel D0 --model          # the recording played on
                                                                    # the Python model instead
    python3 board/listen.py remote.sr --channel D0 --program        # only the receiver's source
    python3 board/listen.py --record 3 --pin 5                      # no analyzer: the chip
                                                                    # records the line itself

The line goes to the board's pin --pin (and, for a clocked line, its clock to --pin + 1); the
recording's times count clocks of --clock-hz, the board's clock (the Tang Nano 20K: 27 MHz).
Each frame is printed as its bits in hex, and the CRC's verdict when the line has a CRC.
State machine 1 is left as it is: a transmitter loaded there on the same pin is received, the
chip learning its own line.

With --record, no logic analyzer: programs/logic/edges.asm on state machine 0 records the line
on --pin for that many seconds (its times strobes of --record-div clocks), the learner reads the
recording, and the receiver it writes replaces edges.asm on the same state machine: one chip,
from recording to receiving. A line of one wire (pulses, frames, asynchronous), whose edges come
no faster than the host empties the RX FIFO, and with no gap of 65536 strobes or more (16-bit
times: a longer one is read short), so for a burst of characters or frames.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Callable

from board import Board, Model, Serial

from asm import assemble                                 # noqa: E402  (board put it on the path)
from chip import CTRL                                    # noqa: E402
from host import Host                                    # noqa: E402
from learn import (Capture, clocked_check, clocked_rx_program, clocked_timing,  # noqa: E402
                   crc_check, frame_timing, frames_rx_program, infer_async, infer_clocked,
                   infer_frames, infer_pulses, pulses_check, pulses_rx_program, pulses_timing,
                   rx_frames, rx_program, timing, word_value)


EDGES = Path(__file__).resolve().parents[1] / "programs" / "logic" / "edges.asm"


def record(host: Host, pin: int, div: int, done: Callable[[], bool]) -> Capture:
    """The line on `pin` recorded by the chip: edges.asm on state machine 0 at CLKDIV `div`,
    the RX FIFO at its largest (253 words), emptied until done(); times in clocks. ValueError
    when no edge came, or the RX FIFO filled (edges.asm then stalls, and edges are lost)."""
    host.load(0, assemble(EDGES.read_text()), clkdiv_int=div, in_base=pin, fifo_split=1)
    host.start(host.read(0, CTRL)[0] | 0b01)
    words: list[int] = []
    full = False
    while not done():
        n = host.rx_level(0)
        full |= n >= 253
        words += host.pop(0, n) if n else []
    host.start(host.read(0, CTRL)[0] & ~0b01)
    words += host.drain(0)
    if full:
        raise ValueError(f"the RX FIFO filled: edges faster than the host reads them "
                         f"({len(words) - 1} recorded)")
    if len(words) < 2:
        raise ValueError(f"no edge on pin {pin}")
    return Capture.from_edges_program(words, div)


def learn(kind: str, caps: dict[str, Capture], channel: str, args) -> tuple:
    """(what was learned, the receiver's source, CLKDIV, fraction, whether it checks a CRC);
    ValueError when the recording is not of that kind."""
    cap = caps[channel]
    if kind == "pulses":
        p = infer_pulses(cap)
        src = pulses_rx_program(p, args.pin)
        return p, src, pulses_timing(p)[0], 0, pulses_check(p) is not None
    if kind == "frames":
        p = infer_frames(cap)
        _, div, frac = frame_timing(p)
        return p, frames_rx_program(p, args.pin), div, frac, crc_check(p) is not None
    if kind == "async":                                  # breaks (LIN, DMX) only when asked for
        p = infer_async(cap, breaks=getattr(args, "kind", None) == "async")
        _, div, frac = timing(p)
        return p, rx_program(p, args.pin), div, frac, False
    pins = {name: caps[name] for name in (channel, args.clock_channel, args.select_channel) if name}
    p = next((q for q in infer_clocked(pins) if q.data == channel), None)
    if p is None:
        raise ValueError(f"{channel} is not data on the clock")
    src = clocked_rx_program(p, args.pin, args.select_pin)
    return p, src, clocked_timing(p)[0], 0, clocked_check(p) is not None


def learn_auto(caps: dict[str, Capture], channel: str, args) -> tuple:
    """(the kind, then as learn()) of the family that explains the recording. Of pulses and
    frames, the one that finds fewer frames, pulses when equal: the wrong family cuts a frame
    at the pauses within it (biphase mark read as pulse widths ends a frame at every long
    space, and keeps half its bits). An asynchronous line when neither explains it.
    ValueError with each family's reason when none does."""
    found, errors = [], []
    for kind in ("pulses", "frames"):
        try:
            found.append((kind, *learn(kind, caps, channel, args)))
        except ValueError as e:
            errors.append(f"{kind}: {e}")
    if found:
        return min(found, key=lambda f: len(f[1].frames))
    try:
        return ("async", *learn("async", caps, channel, args))
    except ValueError as e:
        raise ValueError("\n  ".join(errors + [f"async: {e}"])) from None


def hexbits(b: list[int], lsb: bool) -> str:
    """Whole bytes in hex (each byte's first bit its MSB, or with lsb its LSB), then the bits
    left over."""
    out = []
    for k in range(0, len(b) - 7, 8):
        byte = b[k:k + 8][::-1] if lsb else b[k:k + 8]
        out.append(f"{int(''.join(map(str, byte)), 2):02x}")
    rest = b[len(b) // 8 * 8:]
    return " ".join(out) + (f" +{''.join(map(str, rest))}" if rest else "")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("recording", nargs="?", help="a sigrok session file (.sr)")
    ap.add_argument("--channel", default="D0", help="the line's channel in the recording")
    ap.add_argument("--pin", type=int, default=0, help="the board's pin the line goes to")
    ap.add_argument("--kind", default="auto", choices=("auto", "pulses", "frames", "async",
                                                       "clocked"))
    ap.add_argument("--clock-channel", help="a clocked line's clock (--kind clocked)")
    ap.add_argument("--select-channel", help="a clocked line's select, if it has one")
    ap.add_argument("--select-pin", type=int, help="the board's pin the select goes to")
    ap.add_argument("--clock-hz", type=float, default=27e6, help="the board's clock")
    ap.add_argument("--lsb", action="store_true", help="print bytes LSB first (UART, NEC, ...)")
    ap.add_argument("--port", default="/dev/ttyACM0", help="the Pico bridge")
    ap.add_argument("--hz", type=int, default=1_000_000, help="SCLK")
    ap.add_argument("--model", action="store_true",
                    help="play the recording on the Python model instead of the board")
    ap.add_argument("--program", action="store_true",
                    help="print the receiver's source and stop: no board needed")
    ap.add_argument("--record", type=float, metavar="SECONDS",
                    help="no recording file: the chip records --pin itself for SECONDS")
    ap.add_argument("--record-div", type=int, default=4, metavar="CLKDIV",
                    help="edges.asm's CLKDIV for --record: its times' resolution")
    args = ap.parse_args()
    if (args.recording is None) == (args.record is None):
        ap.error("a recording file, or --record")
    if args.record is not None and (args.clock_channel or args.kind == "clocked" or args.program):
        ap.error("--record: a line of one wire, on the board (or --model)")

    host = None
    if args.record is not None:                         # the board first: it records
        port = Model() if args.model else Serial(args.port)
        host = Host(Board(port, args.hz))
        t_end = time.monotonic() + args.record
        if args.model:                                   # the model's own clock
            c_end = port.chip.cycle + round(args.record * args.clock_hz)
            done = lambda: port.chip.cycle >= c_end      # noqa: E731
        else:
            done = lambda: time.monotonic() >= t_end     # noqa: E731
        print(f"recording pin {args.pin} for {args.record:g} s")
        try:
            cap = record(host, args.pin, args.record_div, done)
        except ValueError as e:
            print(f"nothing recorded: {e}")
            return 1
        print(f"recorded {len(cap.edges)} edges")
        args.channel = "pin"
        caps = {"pin": cap}
    else:
        caps = Capture.from_sigrok(args.recording, args.clock_hz)
    if args.channel not in caps:
        ap.error(f"no channel {args.channel}: {', '.join(caps)}")
    if args.clock_channel:
        args.kind = "clocked"
    try:
        if args.kind == "auto":
            kind, p, src, div, frac, crc = learn_auto(caps, args.channel, args)
        else:
            kind = args.kind
            p, src, div, frac, crc = learn(kind, caps, args.channel, args)
    except ValueError as e:
        print(f"nothing learned:\n  {e}")
        return 1
    print(f"learned {kind}: {p}")
    print(f"receiver on pin {args.pin}, CLKDIV {div} + {frac}/256 at {args.clock_hz / 1e6:g} MHz")
    if args.program:
        print(src)
        return 0

    if host is not None:                               # recorded: the same chip, as long again
        end = port.chip.cycle + round(args.record * args.clock_hz) if args.model else None
    elif args.model:
        play = {args.pin: caps[args.channel]}
        if kind == "clocked":
            play[args.pin + 1] = caps[args.clock_channel]
            if args.select_channel:
                play[args.select_pin] = caps[args.select_channel]
        port = Model(play)
        end = caps[args.channel].span[1]
    else:
        port, end = Serial(args.port), None
    host = host or Host(Board(port, args.hz))
    host.load(0, assemble(src), clkdiv_int=div, clkdiv_frac=frac)
    host.start(host.read(0, CTRL)[0] | 0b01)            # SM 1 left as it is
    if args.model and args.record is None:             # the recording from now on, then the
        port.chip.t0 = port.chip.cycle                     # line at rest a tenth as long: a
        end += port.chip.t0 + end // 10 + 1000             # receiver sees the last frame end
    elif args.model:
        port.chip.t0 = port.chip.cycle
    print("listening (Ctrl-C to stop)" if end is None else "playing the recording")

    words: list[int] = []
    try:
        while end is None or port.chip.cycle < end:
            n = host.rx_level(0)
            if not n:
                if end is None:
                    time.sleep(0.01)
                continue
            got = host.pop(0, n)
            if kind == "async":
                for w in got:
                    v = word_value(p, w)
                    print(f"{v:#04x} {chr(v)!r}" if v < 128 else f"{v:#04x}")
                continue
            words += got
            done = max((i for i, w in enumerate(words) if w & 0xFF), default=-1) + 1
            for b, verdict in rx_frames(words[:done]):
                check = ("  CRC ok" if verdict == 3 else "  CRC WRONG") if crc else ""
                print(f"{len(b):4} bits  {hexbits(b, args.lsb)}{check}")
            words = words[done:]
    except KeyboardInterrupt:
        pass
    host.start(host.read(0, CTRL)[0] & ~0b01)
    return 0


if __name__ == "__main__":
    sys.exit(main())
