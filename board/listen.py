#!/usr/bin/env python3
"""listen.py - From a recording to a receiver on the board: sim/learn/learn.py finds the line's
format in a logic analyzer's recording (a sigrok session file, as PulseView saves it), writes
an openPIO receiver for it, and this loads it on state machine 0 and prints what it receives.

    python3 board/listen.py remote.sr --channel D0 --pin 0          # the Pico on /dev/ttyACM0
    python3 board/listen.py bus.sr --channel D2 --pin 3 --kind frames
    python3 board/listen.py spi.sr --channel D1 --clock-channel D0 --pin 2   # clock on pin 3
    python3 board/listen.py remote.sr --channel D0 --model          # the recording played on
                                                                    # the Python model instead
    python3 board/listen.py remote.sr --channel D0 --program        # only the receiver's source

The line goes to the board's pin --pin (and, for a clocked line, its clock to --pin + 1); the
recording's times count clocks of --clock-hz, the board's clock (the Tang Nano 20K: 27 MHz).
Each frame is printed as its bits in hex, and the CRC's verdict when the line has a CRC.
State machine 1 is left as it is: a transmitter loaded there on the same pin is received, the
chip learning its own line.
"""

from __future__ import annotations

import argparse
import sys
import time

from board import Board, Model, Serial

from asm import assemble                                 # noqa: E402  (board put it on the path)
from chip import CTRL, FIFO_LEVEL                        # noqa: E402
from host import Host                                    # noqa: E402
from learn import (Capture, clocked_check, clocked_rx_program, clocked_timing,  # noqa: E402
                   crc_check, frame_timing, frames_rx_program, infer_async, infer_clocked,
                   infer_frames, infer_pulses, pulses_check, pulses_rx_program, pulses_timing,
                   rx_frames, rx_program, timing, word_value)


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
    if kind == "async":
        p = infer_async(cap)
        _, div, frac = timing(p)
        return p, rx_program(p, args.pin), div, frac, False
    pins = {name: caps[name] for name in (channel, args.clock_channel, args.select_channel) if name}
    p = next((q for q in infer_clocked(pins) if q.data == channel), None)
    if p is None:
        raise ValueError(f"{channel} is not data on the clock")
    src = clocked_rx_program(p, args.pin, args.select_pin)
    return p, src, clocked_timing(p)[0], 0, clocked_check(p) is not None


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
    ap.add_argument("recording", help="a sigrok session file (.sr)")
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
    args = ap.parse_args()

    caps = Capture.from_sigrok(args.recording, args.clock_hz)
    if args.channel not in caps:
        ap.error(f"no channel {args.channel}: {', '.join(caps)}")
    if args.clock_channel:
        args.kind = "clocked"
    kinds = ("pulses", "frames", "async") if args.kind == "auto" else (args.kind,)
    errors = []
    for kind in kinds:
        try:
            p, src, div, frac, crc = learn(kind, caps, args.channel, args)
            break
        except ValueError as e:
            errors.append(f"{kind}: {e}")
    else:
        print("nothing learned:\n  " + "\n  ".join(errors))
        return 1
    print(f"learned {kind}: {p}")
    print(f"receiver on pin {args.pin}, CLKDIV {div} + {frac}/256 at {args.clock_hz / 1e6:g} MHz")
    if args.program:
        print(src)
        return 0

    if args.model:
        play = {args.pin: caps[args.channel]}
        if kind == "clocked":
            play[args.pin + 1] = caps[args.clock_channel]
            if args.select_channel:
                play[args.select_pin] = caps[args.select_channel]
        port = Model(play)
        end = caps[args.channel].span[1]
    else:
        port, end = Serial(args.port), None
    host = Host(Board(port, args.hz))
    host.load(0, assemble(src), clkdiv_int=div, clkdiv_frac=frac)
    host.start(host.read(0, CTRL)[0] | 0b01)            # SM 1 left as it is
    if args.model:                                     # the recording from now on, then the
        port.chip.t0 = port.chip.cycle                     # line at rest a tenth as long: a
        end += port.chip.t0 + end // 10 + 1000             # receiver sees the last frame end
    print("listening (Ctrl-C to stop)" if end is None else "playing the recording")

    words: list[int] = []
    try:
        while end is None or port.chip.cycle < end:
            n = host.read(0, FIFO_LEVEL)[0] >> 8
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
