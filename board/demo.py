#!/usr/bin/env python3
"""demo.py - The demo (DEMO.md, step 6), a step at a time: Enter goes on, Ctrl-C ends it.

    python3 board/demo.py                      # records the remote with sigrok-cli
    python3 board/demo.py --recording remote.sr    # a recording made before: no analyzer

1. The chip is alive: a comet goes round the WS2812 ring (ws2812.asm on SM 1, pin 1).
2. It does not know the remote: SM 0's program memory erased and read back; keys reach pin 0
   and nothing understands them.
3. It listens: the analyzer records the receiver's output (pin 0, D0) for 20 s.
4. It learns: sim/learn/learn.py finds the format and writes a receiver for it; every step of
   it is printed (explain.py): the widths measured, the leader, the bits of each frame, the
   check, the timing, the machine code.
5. It understands: the receiver on SM 0, read back from the chip; each frame it pushes is
   printed as the raw RX FIFO words, its bits, and whether it was in the recording: a key never
   recorded is understood too. The comet takes a colour from it. `r` then Enter learns
   another remote (back to 3).
6. Why it matters.

Everything printed also goes to a log, each line with the time of day (--log), to publish
next to the video. The comet keeps going through every step: the PC only feeds it colours.
"""

from __future__ import annotations

import argparse
import datetime
import re
import select
import subprocess
import sys
import time
from pathlib import Path

from board import Board, Serial
from explain import Clock, bits_hex, explain, sha, us, words_sha

from asm import PAGE_WORDS, assemble                     # noqa: E402  (board put it on the path)
from chip import FIFO_LEVEL, ID, OP_IMEM_RD, OP_IMEM_WR, PINS_IN, header   # noqa: E402
from host import Host                                    # noqa: E402
from learn import AsyncSerial, Capture, Frames, Pulses, rx_frames  # noqa: E402
from listen import learn

ROOT = Path(__file__).resolve().parents[1]
LEDS = 12
HUES = [(32, 8, 48), (48, 0, 0), (0, 48, 0), (0, 0, 48), (40, 24, 0), (0, 32, 32), (40, 0, 32)]
ANSI = re.compile(r"\033\[[0-9;]*m")


class Log:
    """stdout, and a copy in a file without colours, each line after the time of day."""

    def __init__(self, path: Path) -> None:
        self.term, self.file, self.fresh = sys.stdout, open(path, "w", encoding="utf-8"), True

    def write(self, s: str) -> int:
        self.term.write(s)
        for part in re.split(r"(\n)", ANSI.sub("", s)):
            if part and self.fresh and part != "\n":
                self.file.write(datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3] + "  ")
            if part:
                self.file.write(part)
                self.fresh = part == "\n"
        self.file.flush()
        return len(s)

    def flush(self) -> None:
        self.term.flush()


def status(s: str) -> None:
    """A line that changes in place: on the terminal only, not in the log."""
    sys.__stdout__.write(f"\r{s}\033[K")
    sys.__stdout__.flush()


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


def enter() -> str | None:
    """The line typed, once Enter is pressed; None until then, without waiting."""
    if select.select([sys.stdin], [], [], 0)[0]:
        return sys.stdin.readline().strip()
    return None


def step(title: str, text: str) -> None:
    print(f"\n\033[1m{title}\033[0m\n{text}")


def wait(comet: Comet, also=lambda: None, prompt: str = "(Enter)") -> str:
    """Until Enter, the comet going round and `also` called; the line typed."""
    print(f"\033[2m{prompt}\033[0m", flush=True)
    while (line := enter()) is None:
        comet.tick()
        also()
        time.sleep(0.005)
    return line


def imem(host: Host, n: int, count: int) -> list[int]:
    """SM n's first `count` instructions, read from the chip (the SM stopped)."""
    return host.chip.spi(header(OP_IMEM_RD, n), [0] * (count + 1))[2:]


def hexwords(ws: list[int], indent: str = "    ") -> str:
    return "\n".join(f"{indent}{k:02x}: " + " ".join(f"{w:04x}" for w in ws[k:k + 12])
                     for k in range(0, len(ws), 12))


def record(args, path: Path, comet: Comet) -> bool:
    """Step 3 with the analyzer: sigrok-cli records pin 0 to `path`."""
    print("Press about ten different keys, short presses, a second apart.")
    wait(comet, prompt="(Enter to start)")
    rec = subprocess.Popen(["sigrok-cli", "-d", "fx2lafw", "--config", "samplerate=1m",
                            "--time", str(int(args.seconds * 1000)), "--channels",
                            args.channel, "-o", str(path)])
    print(f"recording started, {args.seconds:g} s at 1 MHz")
    end = time.monotonic() + args.seconds
    while rec.poll() is None:
        comet.tick()
        status(f"  recording... {max(0, end - time.monotonic()):4.1f} s")
        time.sleep(0.005)
    status("")
    if rec.returncode:
        print("sigrok-cli failed")
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", default="/dev/ttyACM0", help="the Pico bridge")
    ap.add_argument("--recording", action="append", default=[],
                    help="a recording of a remote made before, no analyzer; again for the "
                         "next remote (r in step 5)")
    ap.add_argument("--save", default="remote.sr", help="where step 3 saves its recording "
                    "(the next remote's: remote-2.sr, ...)")
    ap.add_argument("--seconds", type=float, default=20, help="how long step 3 records")
    ap.add_argument("--channel", default="D0", help="the analyzer's channel on pin 0")
    ap.add_argument("--clock-hz", type=float, default=27e6, help="the board's clock")
    ap.add_argument("--msb", action="store_true", help="print bytes MSB first (not NEC)")
    ap.add_argument("--log", help="the log (default demo-<date>-<time>.log)")
    args = ap.parse_args()
    args.pin, args.clock_channel, args.select_channel, args.select_pin = 0, None, None, None
    log = Path(args.log or datetime.datetime.now().strftime("demo-%Y%m%d-%H%M%S.log"))
    sys.stdout = Log(log)
    hz, lsb = args.clock_hz, not args.msb

    git = subprocess.run(["git", "-C", str(ROOT), "describe", "--always", "--dirty"],
                         capture_output=True, text=True).stdout.strip()
    host = Host(Board(Serial(args.port)))
    host.start(0)
    print(f"openPIO demo, {datetime.date.today()}, repository {git or '?'}, log {log}")
    print(f"the chip on {args.port}: ID {host.read(0, ID)[0]:#06x}, its clock {hz / 1e6:g} MHz")
    ws2812 = assemble((ROOT / "programs/ws2812/ws2812.asm").read_text())
    host.load(1, ws2812, clkdiv_int=3, clkdiv_frac=96, sideset_base=1, set_base=1)
    back = imem(host, 1, len(ws2812))
    host.start(0b10)
    comet = Comet(host)
    try:
        step("1. The chip is alive",
             "A comet on the ring: ws2812.asm, 5 instructions on state machine 1, pin 1.\n"
             "The PC only sends colours, 18 words of 16 bits every 40 ms.\n"
             f"SM 1's program, read back from the chip:\n{hexwords(back)}")
        wait(comet)

        host.chip.spi(header(OP_IMEM_WR, 0), [0] + [0] * PAGE_WORDS)
        empty = imem(host, 0, PAGE_WORDS)
        step("2. It does not know the remote",
             f"SM 0's program memory, erased and read back from the chip: {len(empty)} words, "
             + ("all 0000." if not any(empty) else f"NOT EMPTY: {hexwords(empty)}") + "\n"
             "SM 0 is stopped. Pin 0 is the infrared receiver's output: press keys, the chip\n"
             "sees the line move, and nothing on it understands it.")
        moves, last = 0, None

        def watch() -> None:
            nonlocal moves, last
            pin = host.read(0, PINS_IN)[0] & 1
            moves += last is not None and pin != last
            last = pin
            status(f"  pin 0 changed {moves} times; frames understood: 0")

        wait(comet, watch)
        print(f"pin 0 changed {moves} times while polled; frames understood: 0")

        recordings, take = list(args.recording), 0
        while True:
            take += 1
            step("3. It listens", f"The logic analyzer records pin 0 for {args.seconds:g} s.")
            if recordings:
                path = Path(recordings.pop(0))
                print(f"(a recording made before: {path})")
                wait(comet)
            else:
                save = Path(args.save)
                path = save if take == 1 else save.with_stem(f"{save.stem}-{take}")
                if not record(args, path, comet):
                    return 1
            made = datetime.datetime.fromtimestamp(path.stat().st_mtime)
            print(f"{path}: {path.stat().st_size} bytes, sha256 {sha(path)}, "
                  f"written {made:%H:%M:%S}")

            step("4. It learns", f"sim/learn/learn.py on {path}, now; every step it takes:")
            clock = Clock()
            say = lambda s: print(f"{clock} {s}")                  # noqa: E731
            caps = Capture.from_sigrok(path, hz)
            cap = caps[args.channel]
            say(f"read {path}: {us(cap.span[1], hz)}, {len(cap.edges)} edges on "
                f"{args.channel}, times in clocks of {hz / 1e6:g} MHz")
            for kind in ("pulses", "frames", "async"):
                try:
                    p, src, div, frac, crc = learn(kind, caps, args.channel, args)
                    break
                except ValueError as e:
                    say(f"not {kind}: {e}")
            else:
                print("nothing learned")
                wait(comet, prompt="(Enter: record again)")
                continue
            say(f"\033[1mlearned: {kind}\033[0m")
            codes = explain(cap, kind, p, src, div, frac, hz, lsb, say)
            say("the receiver's source, as learn.py wrote it:")
            print(src)
            say(f"\033[1mlearned in {time.perf_counter() - clock.t0:.2f} s\033[0m, from the "
                f"recording alone: {describe(p, hz)}")
            wait(comet)

            step("5. It understands",
                 "The program it wrote, on state machine 0. Press keys: each frame is printed\n"
                 "as the chip pushes it, and the comet takes its colour. Keys never recorded\n"
                 "too, then a remote nobody chose.")
            prog = assemble(src)
            host.load(0, prog, clkdiv_int=div, clkdiv_frac=frac)
            back = imem(host, 0, len(prog))
            same = "the same" if back == list(prog) else "DIFFERENT"
            print(f"SM 0's program, read back from the chip: {len(back)} words, {same} as "
                  f"written (sha256 {words_sha(back)})")
            print(f"CLKDIV {div} + {frac}/256; SM 0 and SM 1 running")
            host.start(0b11)
            got: list[int] = []
            t0 = time.monotonic()

            def receive() -> None:
                n = host.read(0, FIFO_LEVEL)[0] >> 8
                if not n:
                    return
                got.extend(host.pop(0, n))
                done = max((i for i, w in enumerate(got) if w & 0xFF), default=-1) + 1
                if not done:
                    return
                print(f"\033[2m  +{time.monotonic() - t0:6.2f} s  SM 0 RX FIFO: "
                      + " ".join(f"{w:04x}" for w in got[:done]) + "\033[0m")
                for b, verdict in rx_frames(got[:done]):
                    ok = verdict == 3 if crc else True
                    new = ("in the recording" if b in codes
                           else "\033[1mNEW: not in the recording\033[0m")
                    print(f"{len(b):4} bits  {bits_hex(b, lsb)}"
                          + (("  CRC ok" if ok else "  CRC WRONG") if crc else "")
                          + f"  {new}", flush=True)
                    if ok and b:
                        comet.hue = HUES[int("".join(map(str, b)), 2) % len(HUES)]
                del got[:done]

            if kind == "async":
                print("(an asynchronous line: see listen.py)")
            line = wait(comet, receive if kind != "async" else lambda: None,
                        "(Enter: go on; r and Enter: learn another remote)")
            if line.lower() != "r":
                break
            host.start(0b10)                                   # SM 0 stopped: back to 3

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
    print(f"log: {log}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
