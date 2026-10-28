"""Gate-level lockstep co-simulation: the RTL of src/ as a flat netlist, stepped in Python.

Yosys synthesizes the RTL to $_AND_/$_OR_/$_XOR_/$_NOT_/$_MUX_ and $_DFF_P_ cells; the netlist is
compiled into straight-line Python, so a test bench steps it one clock at a time beside the
reference model (sim/model/) and compares them.

Yosys: $YOSYS, else `yosys`, else `yowasp-yosys` (pip install yowasp-yosys).
RTL: src/, or $OPENPIO_SRC (e.g. a mutated copy for harness sensitivity checks).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sim" / "model"))

SRC = Path(os.environ.get("OPENPIO_SRC", ROOT / "src"))


def find_yosys() -> str:
    for cand in (os.environ.get("YOSYS"), "yosys", "yowasp-yosys"):
        if cand and shutil.which(cand):
            return shutil.which(cand)
    raise RuntimeError("Yosys not found: set $YOSYS or pip install yowasp-yosys")


def build(sources: list[str], top: str) -> dict:
    """Lint (check -assert) and synthesize src/<sources> to a gate-level JSON module."""
    with tempfile.TemporaryDirectory() as tmp:
        # yowasp-yosys (WASM) only sees the working directory
        for f in sources:
            shutil.copy(SRC / f, tmp)
        script = (f"read_verilog -noautowire {' '.join(sources)}; "
                  f"hierarchy -check -top {top}; proc; check -assert; "
                  f"synth -noabc -flatten -top {top}; dffunmap; opt_clean; write_json net.json")
        subprocess.run([find_yosys(), "-q", "-p", script], cwd=tmp, check=True,
                       stdout=subprocess.DEVNULL)
        return json.load(open(Path(tmp) / "net.json"))["modules"][top]


class Netlist:
    """Compiled two-phase simulator: comb(v) settles logic, clk(v) updates DFFs.

    Signals are addressed by port name or by flattened net name
    (e.g. "u_core.dbg_pc").
    """

    def __init__(self, module: dict, xval: int = 0) -> None:
        self.ports = {n: p["bits"] for n, p in module["netnames"].items()}
        self.ports.update({n: p["bits"] for n, p in module["ports"].items()})
        cells = [c for c in module["cells"].values() if c["type"] != "$scopeinfo"]
        comb = [c for c in cells if c["type"] != "$_DFF_P_"]
        ffs = [c for c in cells if c["type"] == "$_DFF_P_"]

        nets = set()
        for c in cells:
            for b in c["connections"].values():
                nets.update(x for x in b if not isinstance(x, str))
        for bits in self.ports.values():
            nets.update(x for x in bits if not isinstance(x, str))
        idx = {n: i for i, n in enumerate(sorted(nets))}
        self.size = len(idx)

        def ref(b):
            # 'x' constants are unreachable don't-cares left by Yosys shift mapping
            if isinstance(b, str):
                return str(xval if b == "x" else int(b))
            return f"v[{idx[b]}]"

        drv = {c["connections"]["Y"][0]: c for c in comb}
        order, seen = [], set()
        for root in comb:
            stack = [(root, False)]
            while stack:
                cur, done = stack.pop()
                if done:
                    order.append(cur)
                    continue
                if id(cur) in seen:
                    continue
                seen.add(id(cur))
                stack.append((cur, True))
                for pn, b in cur["connections"].items():
                    if pn != "Y" and not isinstance(b[0], str) and b[0] in drv:
                        stack.append((drv[b[0]], False))

        ops = {"$_NOT_": "1-{A}", "$_AND_": "{A}&{B}", "$_OR_": "{A}|{B}",
               "$_XOR_": "{A}^{B}", "$_MUX_": "({B} if {S} else {A})"}
        lines = ["def comb(v):"]
        for c in order:
            p = c["connections"]
            args = {k: ref(p[k][0]) for k in p if k != "Y"}
            lines.append(f"    {ref(p['Y'][0])}={ops[c['type']].format(**args)}")
        lines.append("def clk(v):")
        qs = ",".join(ref(c["connections"]["Q"][0]) for c in ffs)
        ds = ",".join(ref(c["connections"]["D"][0]) for c in ffs)
        lines.append(f"    {qs},={ds}," if ffs else "    pass")
        ns: dict = {}
        exec("\n".join(lines), ns)
        self.comb, self.clk = ns["comb"], ns["clk"]
        self.pidx = {k: [b if isinstance(b, str) else idx[b] for b in bits]
                     for k, bits in self.ports.items()}


class Core:
    def __init__(self, net: Netlist) -> None:
        self.net = net
        self.v = [0] * net.size

    def set(self, name: str, val: int) -> None:
        for i, b in enumerate(self.net.pidx[name]):
            self.v[b] = (int(val) >> i) & 1

    def get(self, name: str, x_fill: int | None = None) -> int:
        """Read a signal; bits Yosys removed ('x') raise unless x_fill supplies them."""
        r = 0
        for i, b in enumerate(self.net.pidx[name]):
            if b == "x":
                if x_fill is None:
                    raise ValueError(f"{name}[{i}] was optimized away")
                bit = (x_fill >> i) & 1
            else:
                bit = int(b) if isinstance(b, str) else self.v[b]
            r |= bit << i
        return r

    def eval(self) -> None:
        self.net.comb(self.v)

    def clock(self) -> None:
        self.net.comb(self.v)
        self.net.clk(self.v)
        self.net.comb(self.v)
