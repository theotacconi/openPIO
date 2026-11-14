#!/usr/bin/env python3
"""explain.py - What the learner saw and did, step by step: the recording's pulses as
measured, how they group, the leader, which pulse carries the bit, each frame's bits with its
time in the recording, the CRC search, the receiver's timing and its machine code. demo.py
prints it during the demo; on its own it explains a recording, no board needed:

    python3 board/explain.py board/captures/nec_remote.sr --channel D0
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sim" / "learn"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sim" / "model"))

from asm import assemble                                 # noqa: E402
from learn import (LEADER_MIN, Capture, Pulses, groups, infer_async, infer_frames,  # noqa: E402
                   infer_pulses, pulses_timing)

DIM, BOLD, OFF = "\033[2m", "\033[1m", "\033[0m"


def us(clocks: float, hz: float) -> str:
    """Clocks as a time: µs under 10 ms, else ms or s."""
    t = clocks / hz
    return f"{t * 1e6:.0f} µs" if t < 0.01 else f"{t * 1e3:.1f} ms" if t < 1 else f"{t:.2f} s"


def bits_hex(b: list[int], lsb: bool = True) -> str:
    """Whole bytes in hex (lsb: each byte's first bit its LSB), then the bits left over."""
    out = [f"{int(''.join(map(str, b[k:k + 8][::-1] if lsb else b[k:k + 8])), 2):02x}"
           for k in range(0, len(b) - 7, 8)]
    rest = b[len(b) // 8 * 8:]
    return " ".join(out) + (f" +{''.join(map(str, rest))}" if rest else "")


class Clock:
    """Elapsed time since it was made, as a log prefix."""

    def __init__(self) -> None:
        self.t0 = time.perf_counter()

    def __str__(self) -> str:
        return f"{DIM}[+{time.perf_counter() - self.t0:6.3f} s]{OFF}"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def words_sha(words: list[int]) -> str:
    """The sha256 of 16-bit words, big-endian: a program's, as written and as read back."""
    return hashlib.sha256(b"".join(w.to_bytes(2, "big") for w in words)).hexdigest()[:16]


def levels(cap: Capture) -> list[tuple[int, int, int]]:
    """(start clock, width, level) of each pulse between two edges."""
    e = cap.edges
    return [(e[k], e[k + 1] - e[k], cap.level ^ (k + 1 & 1)) for k in range(len(e) - 1)]


def table(ws: list[int], hz: float, cut: float | None = None) -> list[str]:
    """One line a group of widths: how many, the typical width, the spread; the widths of `cut`
    or more in one line, as pauses."""
    out, pauses = [], [w for w in ws if cut and w >= cut]
    for a, b in groups(w for w in ws if not cut or w < cut):
        g = sorted(w for w in ws if a <= w <= b)
        out.append(f"{us(g[len(g) // 2], hz):>10} x{len(g):<5} ({us(a, hz)} .. {us(b, hz)})")
    if pauses:
        out.append(f"{'pauses':>10} x{len(pauses):<5} ({us(min(pauses), hz)} .. "
                   f"{us(max(pauses), hz)}): between frames")
    return out


def timeline(span: float, marks: list[tuple[float, str]], cols: int = 64) -> str:
    """The recording as a strip of `cols` characters, each frame a character where it starts."""
    row = ["·"] * cols
    for t, ch in marks:
        k = min(cols - 1, int(t / span * cols))
        row[k] = ch if row[k] in "·░" else row[k]
    return "".join(row)


def bursts(cap: Capture, idle: int, cut: float, lead: int) -> list[tuple[int, list[int]]]:
    """(start clock, widths) of each burst: from a mark to `cut` clocks or more at rest past its
    first `lead` pulses (the leader's space may be as long as a pause, as the receiver
    sees it)."""
    out, cur = [], None
    for t, w, lv in levels(cap):
        if lv == idle and w >= cut and (cur is None or len(cur[1]) >= lead):
            if cur:
                out.append(cur)
            cur = None
        elif cur is not None:
            cur[1].append(w)
        elif lv != idle:
            cur = (t, [w])
    if cur:
        out.append(cur)
    return out


def decode(p: Pulses, ws: list[int]) -> list[int] | None:
    """The bits of a burst by what was learned; None when it does not start with the leader."""
    n = len(p.leader)
    hi = p.leader_max or [w / LEADER_MIN ** 2 for w in p.leader]
    if len(ws) < n or any(not lo * LEADER_MIN <= w <= h / LEADER_MIN
                          for lo, h, w in zip(p.leader, hi, ws)):
        return None
    t = (p.short + p.long) / 2
    vals = ws[n::2] if p.carrier == "mark" else ws[n + 1::2]
    return [int(w > t) ^ (1 - p.one) for w in vals]


def explain_pulses(cap: Capture, p: Pulses, hz: float, lsb: bool, say) -> list[list[int]]:
    """The pulse code's trace; the bits of every frame in the recording."""
    cap = cap.despiked()
    span = (cap.span or (0, cap.edges[-1]))[1]
    act, rest = ("low", "high") if p.idle else ("high", "low")
    t = (p.short + p.long) / 2
    div, _, (m, v), n = pulses_timing(p)
    cut = n * m * v * div                     # the receiver's end of a frame
    ps = levels(cap)
    say(f"the line rests {rest} (its longest pulse): a mark is {act}, a space {rest}")
    say(f"{BOLD}marks{OFF} ({act}), measured:")
    for line in table([w for _, w, lv in ps if lv != p.idle], hz):
        say("    " + line)
    say(f"{BOLD}spaces{OFF} ({rest}), measured:")
    for line in table([w for _, w, lv in ps if lv == p.idle], hz, p.gap):
        say("    " + line)
    say(f"a frame ends at {us(cut, hz)} at rest after its leader")

    lead = " + ".join(f"{('mark', 'space')[k & 1]} {us(w, hz)}" for k, w in enumerate(p.leader))
    say(f"leader, what every frame of the main kind starts with: {lead or 'none'}")
    other = ("spaces", "marks")[p.carrier == "space"]
    say(f"after it the {other} have one width and the {p.carrier}s two, {us(p.short, hz)} and "
        f"{us(p.long, hz)}: {BOLD}the {p.carrier} carries the bit{OFF}, a "
        f"{('short', 'long')[p.one]} one is a 1 (threshold {us(t, hz)})")

    bs = bursts(cap, p.idle, cut, len(p.leader))
    learned = [b for b in bs if decode(p, b[1])]
    if learned:
        _, ws = learned[0]
        n = len(p.leader)
        say(f"the first frame, as measured ({us(learned[0][0], hz)} into the recording):")
        say("    " + " ".join(f"{('M', 'S')[k & 1]}{w / hz * 1e6:.0f}" for k, w in
                              enumerate(ws[:n])) + "  (leader)")
        pairs, k = [], len(p.leader)
        while k + 1 < len(ws) and len(pairs) < 16:
            v = ws[k] if p.carrier == "mark" else ws[k + 1]
            pairs.append(f"M{ws[k] / hz * 1e6:.0f} S{ws[k + 1] / hz * 1e6:.0f}"
                         f"->{int(v > t) ^ (1 - p.one)}")
            k += 2
        for row in (pairs[:8], pairs[8:]):
            say("    " + "  ".join(row))
        say("    ...")

    say(f"{BOLD}every burst in the recording{OFF}, decoded by what was learned:")
    seen: dict[tuple, int] = {}
    for start, ws in bs:
        b = decode(p, ws)
        when = f"{start / hz:6.2f} s"
        if b is None:
            say(f"    {when}  {len(ws):3} pulses  {DIM}not this leader "
                f"(M{ws[0] / hz * 1e6:.0f}{f' S{ws[1] / hz * 1e6:.0f}' if len(ws) > 1 else ''}"
                f"): a repeat code, or another kind{OFF}")
        elif not b:
            say(f"    {when}  {DIM}the leader alone{OFF}")
        else:
            seen[tuple(b)] = seen.get(tuple(b), 0) + 1
            used = "" if b in p.frames else f"  {DIM}(left out: a width few frames have){OFF}"
            say(f"    {when}  {len(b):3} bits   {bits_hex(b, lsb)}{used}")
    say(f"    {DIM}{timeline(span, [(s, '█' if decode(p, w) else '░') for s, w in bs])}"
        f"{OFF}  0 .. {us(span, hz)}")
    say(f"{len(seen)} different codes in {sum(seen.values())} frames"
        + (f"; {p.ignored} bursts of another kind left out" if p.ignored else ""))
    return [list(b) for b in seen]


def explain_check(p, say) -> None:
    if p.crc:
        say(f"{BOLD}a check found{OFF}: {p.crc}; every frame agrees, so the receiver checks it "
            "on the chip (its PAU computes the CRC bit by bit)")
    else:
        say("no CRC found: no polynomial of width 1..32 (with init and final XOR 0 or all ones,"
            " from any start) divides every frame; the receiver passes the bits on unchecked")


def explain_timing(p: Pulses, hz: float, say) -> None:
    div, leader, (m, v), n = pulses_timing(p)
    s = div / hz
    say(f"{BOLD}for the chip{OFF}: CLKDIV {div}, a strobe every {s * 1e6:.2f} µs; the program "
        f"times each pulse with WAIT ... TIMEOUT:")
    for k, ((lm, lv), (xm, xv)) in enumerate(leader):
        say(f"    leader {('mark', 'space')[k & 1]}: at least {lm * lv} strobes "
            f"({lm * lv * s * 1e6:.0f} µs), ending within {xm * xv} more "
            f"({xm * xv * s * 1e6:.0f} µs): another remote's leader is refused")
    say(f"    a {p.carrier} past T = {m * v} strobes ({m * v * s * 1e6:.0f} µs) is long; "
        f"{n} T without a mark ({n * m * v * s * 1e3:.1f} ms) end a frame")


def explain_program(src: str, say) -> list[int]:
    words = list(assemble(src))
    say(f"{BOLD}the receiver{OFF}: {len(words)} instructions of 16 bits, machine code "
        f"(sha256 {words_sha(words)}):")
    for k in range(0, len(words), 12):
        say(f"    {k:02x}: " + " ".join(f"{w:04x}" for w in words[k:k + 12]))
    return words


def explain(cap: Capture, kind: str, p, src: str, div: int, frac: int, hz: float, lsb: bool,
            say) -> list[list[int]]:
    """The whole trace after learning; the codes in the recording (pulse codes only)."""
    codes: list[list[int]] = []
    if kind == "pulses":
        codes = explain_pulses(cap, p, hz, lsb, say)
        explain_check(p, say)
        explain_timing(p, hz, say)
    elif kind == "frames":
        say(f"self-clocked frames: {p}")
        for b in p.frames:
            say(f"    {len(b):3} bits   {bits_hex(b, lsb)}")
        explain_check(p, say)
        codes = [list(b) for b in p.frames]
    else:
        say(f"learned: {p}")
    say(f"CLKDIV {div} + {frac}/256 at {hz / 1e6:g} MHz")
    explain_program(src, say)
    return codes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("recording")
    ap.add_argument("--channel", default="D0")
    ap.add_argument("--clock-hz", type=float, default=27e6)
    ap.add_argument("--pin", type=int, default=0)
    ap.add_argument("--msb", action="store_true", help="bytes MSB first (not NEC)")
    args = ap.parse_args()
    from listen import learn
    args.clock_channel = args.select_channel = args.select_pin = None
    path = Path(args.recording)
    clock = Clock()
    say = lambda s: print(f"{clock} {s}")                          # noqa: E731
    caps = Capture.from_sigrok(path, args.clock_hz)
    cap = caps[args.channel]
    span = (cap.span or (0, 0))[1]
    say(f"read {path}: {us(span, args.clock_hz)}, {len(cap.edges)} edges on {args.channel} "
        f"(sha256 {sha(path)})")
    for kind in ("pulses", "frames", "async"):
        try:
            p, src, div, frac, _ = learn(kind, caps, args.channel, args)
            break
        except ValueError as e:
            say(f"not {kind}: {e}")
    else:
        return 1
    say(f"learned: {kind}")
    explain(cap, kind, p, src, div, frac, args.clock_hz, not args.msb, say)
    return 0


if __name__ == "__main__":
    sys.exit(main())
