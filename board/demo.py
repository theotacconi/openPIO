#!/usr/bin/env python3
"""demo.py - The demo (DEMO.md, step 6), a step at a time: Enter goes on, Ctrl-C ends it.

    python3 board/demo.py                      # records the remote with sigrok-cli
    python3 board/demo.py --recording remote.sr    # a recording made before: no analyzer

1. The chip is alive: a comet goes round the WS2812 ring (ws2812.asm on SM 1, pin 1).
2. It does not know the remote: nothing on SM 0; keys do nothing.
3. It listens: the analyzer records the receiver's output (pin 0, D0) for 20 s.
4. It learns: sim/learn/learn.py finds the format and writes a receiver for it.
5. It understands: the receiver on SM 0 prints each key, and the comet takes a colour from it.
6. Why it matters.

The comet keeps going through every step: the PC only feeds it colours.
"""

from __future__ import annotations

import argparse
import select
import subprocess
import sys
import time
from pathlib import Path

from board import Board, Serial
from listen import hexbits, learn

from asm import assemble                                 # noqa: E402  (board put it on the path)
from chip import FIFO_LEVEL                              # noqa: E402
from host import Host                                    # noqa: E402
from learn import AsyncSerial, Capture, Frames, Pulses, rx_frames  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
LEDS = 12
HUES = [(32, 8, 48), (48, 0, 0), (0, 48, 0), (0, 0, 48), (40, 24, 0), (0, 32, 32), (40, 0, 32)]


def words(colours) -> list[int]:
    """WS2812 colours, (R, G, B) a LED, as ws2812.asm takes them: G R B, 16 bits a word."""
    bits = "".join(f"{g:08b}{r:08b}{b:08b}" for r, g, b in colours)
    bits += "0" * (-len(bits) % 16)
    return [int(bits[i:i + 16], 2) for i in range(0, len(bits), 16)]


class Comet:
    """Four LEDs going round, a step every 40 ms, in the colour of `hue`."""

    def __init__(self, host: Host) -> None:
        self.host, self.step, self.hue, self.next = host, 0, HUES[0], 0.0

    def tick(self) -> None:
        if time.monotonic() < self.next:
            return
        self.next = time.monotonic() + 0.04
        ring = [(0, 0, 0)] * LEDS
        for k in range(4):
            ring[(self.step - k) % LEDS] = tuple(c >> k for c in self.hue)
        self.host.push(1, words(ring))
        self.step += 1

    def off(self) -> None:
        self.host.push(1, words([(0, 0, 0)] * LEDS))


def describe(p, hz: float) -> str:
    """What was learned, in microseconds and bits a second rather than clocks."""
    us = lambda clocks: f"{clocks / hz * 1e6:.0f}"                 # noqa: E731
    crc = f", {p.crc}".split(", from bit")[0] if getattr(p, "crc", None) else ", no CRC"
    if isinstance(p, Pulses):
        lengths = sorted({len(f) for f in p.frames})
        what = "pulse width, marks" if p.carrier == "mark" else "pulse distance, spaces"
        lead = " + ".join(us(w) for w in p.leader) + " µs" if p.leader else "none"
        return (f"{what} of {us(p.short)} / {us(p.long)} µs (a {('short', 'long')[p.one]} one "
                f"is a 1), idle {'high' if p.idle else 'low'}, leader {lead}, "
                f"{'/'.join(map(str, lengths))} bits a frame{crc}"
                + (f"; {p.ignored} frames of another kind left out" if p.ignored else ""))
    if isinstance(p, Frames):
        return (f"{p.code}{' inverted' if p.inv else ''}, {hz / (2 * p.half):.0f} bit/s, "
                f"{'stuffing' if p.stuff else 'no stuffing'}{crc}")
    if isinstance(p, AsyncSerial):
        return f"{str(p).split(',')[0]}, {hz / p.bit:.0f} baud"
    return str(p)


def enter() -> bool:
    """Whether Enter was pressed, without waiting."""
    if select.select([sys.stdin], [], [], 0)[0]:
        sys.stdin.readline()
        return True
    return False


def step(title: str, text: str) -> None:
    print(f"\n\033[1m{title}\033[0m\n{text}")


def wait(comet: Comet, also=lambda: None) -> None:
    """Until Enter, the comet going round and `also` called."""
    print("\033[2m(Enter)\033[0m", flush=True)
    while not enter():
        comet.tick()
        also()
        time.sleep(0.005)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", default="/dev/ttyACM0", help="the Pico bridge")
    ap.add_argument("--recording", help="a recording of the remote made before: no analyzer")
    ap.add_argument("--save", default="remote.sr", help="where step 3 saves its recording")
    ap.add_argument("--seconds", type=float, default=20, help="how long step 3 records")
    ap.add_argument("--channel", default="D0", help="the analyzer's channel on pin 0")
    ap.add_argument("--clock-hz", type=float, default=27e6, help="the board's clock")
    ap.add_argument("--msb", action="store_true", help="print bytes MSB first (not NEC)")
    args = ap.parse_args()
    args.pin, args.clock_channel, args.select_channel, args.select_pin = 0, None, None, None

    host = Host(Board(Serial(args.port)))
    host.start(0)
    host.load(1, assemble((ROOT / "programs/ws2812/ws2812.asm").read_text()), clkdiv_int=3,
              clkdiv_frac=96, sideset_base=1, set_base=1)
    host.start(0b10)
    comet = Comet(host)
    try:
        step("1. The chip is alive",
             "A comet on the ring: ws2812.asm, 5 instructions on state machine 1, pin 1.\n"
             "The PC only sends colours, 18 words of 16 bits every 40 ms.")
        wait(comet)

        step("2. It does not know the remote",
             "Nothing is loaded on state machine 0, which watches pin 0, the infrared\n"
             "receiver's output. Press keys: nothing happens.")
        wait(comet)

        step("3. It listens", f"The logic analyzer records pin 0 for {args.seconds:g} s.")
        if args.recording:
            path = Path(args.recording)
            print(f"(a recording made before: {path})")
            wait(comet)
        else:
            path = Path(args.save)
            print("Press about ten different keys, short presses, a second apart. "
                  "\033[2m(Enter to start)\033[0m", flush=True)
            wait(comet)
            rec = subprocess.Popen(["sigrok-cli", "-d", "fx2lafw", "--config", "samplerate=1m",
                                    "--time", str(int(args.seconds * 1000)), "--channels",
                                    args.channel, "-o", str(path)])
            end = time.monotonic() + args.seconds
            while rec.poll() is None:
                comet.tick()
                left = max(0, end - time.monotonic())
                print(f"\r  recording... {left:4.1f} s ", end="", flush=True)
                time.sleep(0.005)
            print(f"\r  saved {path}" + " " * 12)
            if rec.returncode:
                print("sigrok-cli failed")
                return 1

        step("4. It learns", f"python3 board/listen.py {path} --channel {args.channel} "
                             f"--pin 0 --program")
        caps = Capture.from_sigrok(path, args.clock_hz)
        errors = []
        for kind in ("pulses", "frames", "async"):
            try:
                p, src, div, frac, crc = learn(kind, caps, args.channel, args)
                break
            except ValueError as e:
                errors.append(f"{kind}: {e}")
        else:
            print("nothing learned:\n  " + "\n  ".join(errors))
            return 1
        print("\n".join(line for line in src.splitlines() if not line.startswith(";")))
        print(f"\nlearned: {describe(p, args.clock_hz)}")
        print(f"\033[1m{len(assemble(src))} instructions\033[0m, written from the recording "
              f"alone")
        wait(comet)

        step("5. It understands",
             "The program it wrote, on state machine 0. Press keys: each one is printed, and\n"
             "the comet takes its colour. Then a remote nobody chose.")
        host.load(0, assemble(src), clkdiv_int=div, clkdiv_frac=frac)
        host.start(0b11)
        got: list[int] = []

        def receive() -> None:
            n = host.read(0, FIFO_LEVEL)[0] >> 8
            if not n:
                return
            got.extend(host.pop(0, n))
            done = max((i for i, w in enumerate(got) if w & 0xFF), default=-1) + 1
            for b, verdict in rx_frames(got[:done]):
                ok = verdict == 3 if crc else True
                print(f"{len(b):4} bits  {hexbits(b, not args.msb)}"
                      + (("  CRC ok" if ok else "  CRC WRONG") if crc else ""), flush=True)
                if ok and b:
                    comet.hue = HUES[int("".join(map(str, b)), 2) % len(HUES)]
            del got[:done]

        if kind == "async":
            print("(an asynchronous line: see listen.py)")
        wait(comet, receive if kind != "async" else lambda: None)

        step("6. Why it matters",
             "The same 16-bit instructions, the same two state machines, the same chip, for\n"
             "every protocol in programs/ and for a line nobody wrote a program for.")
        wait(comet)
    except KeyboardInterrupt:
        print()
    finally:
        comet.off()
        time.sleep(0.01)                               # the ring latches dark, then stop
        host.start(0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
