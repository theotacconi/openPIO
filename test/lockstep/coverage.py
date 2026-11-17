#!/usr/bin/env python3
"""coverage.py - What the lockstep benches exercise of the RTL: Verilator's line, branch and
toggle coverage, from the counts the benches write with $OPENPIO_COVERAGE set (vsim.py).

    cd test/lockstep
    export OPENPIO_COVERAGE=obj_dir/coverage
    python3 chip_lockstep.py --runs 40 --cycles 5000
    python3 top_lockstep.py --runs 10 --cycles 20000
    python3 programs.py
    python3 coverage.py obj_dir/coverage              # a table by source file
    python3 coverage.py obj_dir/coverage --uncovered  # and every line or branch never taken

(in the dev container: ../../scripts/dev.sh env OPENPIO_COVERAGE=... python3 ...). A point is
covered when any bench reached it once: a line run, a branch taken, a bit of a signal toggled
(both ways). The state machines (and FIFOs) count as one: a point is covered when any of them
reached it.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

POINT = re.compile(r"^C '(.*)' (\d+)$")


def points(directory: Path) -> dict[tuple, tuple[dict, int]]:
    """Every coverage point of the RTL in the .dat files, its counts summed over them: a point
    is a place in a source file, whichever bench's top it was reached under (the chip's
    `openpio` in chip_lockstep.py, `top_tb` in top_lockstep.py)."""
    out: dict[tuple, tuple[dict, int]] = {}
    for dat in sorted(directory.glob("*.dat")):
        for line in dat.read_text(errors="replace").splitlines():
            m = POINT.match(line)
            if not m:
                continue
            fields = dict(kv.split("\x02", 1) for kv in m[1].split("\x01") if "\x02" in kv)
            key = tuple(sorted((k, v) for k, v in fields.items() if k != "h"))
            old = out.get(key)
            out[key] = (fields, (old[1] if old else 0) + int(m[2]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("directory", type=Path, help="the $OPENPIO_COVERAGE directory")
    ap.add_argument("--uncovered", action="store_true", help="list the lines and branches never taken")
    args = ap.parse_args()
    pts = points(args.directory)
    if not pts:
        print(f"no coverage counts in {args.directory}")
        return 1
    table: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    missed = []
    for fields, n in pts.values():
        f, kind = Path(fields.get("f", "?")).name, fields.get("t", "?")
        kind = "toggle" if kind == "toggle" else "line" if kind in ("line", "block") else kind
        table[f][kind][0] += n > 0
        table[f][kind][1] += 1
        if not n and kind != "toggle":
            missed.append((f, int(fields.get("l", 0)), kind, fields.get("o", ""), fields.get("h", "")))
    kinds = ["line", "branch", "toggle"]
    print(f"{'file':18}" + "".join(f"{k:>22}" for k in kinds))
    total = defaultdict(lambda: [0, 0])
    for f in sorted(table):
        cells = []
        for k in kinds:
            c, t = table[f][k]
            total[k][0] += c
            total[k][1] += t
            cells.append(f"{c:>6}/{t:<6} {100 * c / t:5.1f}%" if t else f"{'-':>22}")
        print(f"{f:18}" + "".join(f"{c:>22}" for c in cells))
    print(f"{'all':18}" + "".join(f"{total[k][0]:>6}/{total[k][1]:<6} {100 * total[k][0] / total[k][1]:5.1f}%"
                                  if total[k][1] else f"{'-':>22}" for k in kinds))
    if args.uncovered:
        print()
        for f, line, kind, comment, where in sorted(missed):
            print(f"{f}:{line}  {kind} {comment}  ({where})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
