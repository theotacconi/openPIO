"""vsim.py - an RTL top of src/ compiled by Verilator into a shared library and stepped from
Python (ctypes), a clock at a time: the lockstep benches run the RTL beside the model with it.

    sim = VSim(["pau.v", "openpio_sm.v", "fifo.v", "openpio.v"], "openpio", {"N_SM": 2})
    sim["rst_n"] = 0; sim.tick()
    sim["h_op"] = 3; sim.eval(); sim["h_rdata"]

Ports are read and written by name (up to 64 bits). eval() settles the logic after inputs
change; tick() is one rising clock edge. The library is built once per source hash in
test/lockstep/obj_dir/ (git-ignored). Verilator: $VERILATOR, else `verilator` (the dev
container, scripts/dev.sh).
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = Path(os.environ.get("OPENPIO_SRC", ROOT / "src"))
BUILD = Path(__file__).resolve().parent / "obj_dir"

PORT = re.compile(r"VL_(IN|OUT|INOUT)(8|16|64|)\(&(\w+),(\d+),(\d+)\);")


def _build(sources: list[str], top: str, params: dict) -> tuple[Path, list[dict]]:
    h = hashlib.sha256(json.dumps([top, sorted(params.items())]).encode())
    for f in sources:
        h.update((SRC / f).read_bytes())
    out = BUILD / f"{top}_{h.hexdigest()[:12]}"
    lib, meta = out / "libsim.so", out / "ports.json"
    if lib.exists() and meta.exists():
        return lib, json.loads(meta.read_text())
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    verilator = os.environ.get("VERILATOR", "verilator")
    args = [verilator, "--cc", "-O3", "--x-assign", "0", "--x-initial", "0", "--top-module", top,
            "--Mdir", str(out), *(f"-G{k}={v}" for k, v in params.items()),
            *(str(SRC / f) for f in sources)]
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL)
    ports = [{"name": m[3], "dir": m[1], "bits": int(m[4]) + 1}
             for m in PORT.finditer((out / f"V{top}.h").read_text())]
    cases_get = "\n".join(f"    case {i}: return t->{p['name']};" for i, p in enumerate(ports))
    cases_set = "\n".join(f"    case {i}: t->{p['name']} = v; break;" for i, p in enumerate(ports)
                          if p["dir"] != "OUT")
    (out / "shim.cpp").write_text(f"""#include "V{top}.h"
#include "verilated.h"
#include <cstdint>
double sc_time_stamp() {{ return 0; }}
extern "C" {{
void* sim_new() {{ return new V{top}; }}
void sim_eval(void* p) {{ static_cast<V{top}*>(p)->eval(); }}
uint64_t sim_get(void* p, int id) {{
    V{top}* t = static_cast<V{top}*>(p);
    switch (id) {{
{cases_get}
    }}
    return 0;
}}
void sim_set(void* p, int id, uint64_t v) {{
    V{top}* t = static_cast<V{top}*>(p);
    switch (id) {{
{cases_set}
    }}
}}
}}
""")
    subprocess.run([verilator, "--cc", "--build", "-O3", "--x-assign", "0", "--x-initial", "0",
                    "--top-module", top, "--Mdir", str(out), *(f"-G{k}={v}" for k, v in params.items()),
                    "-CFLAGS", "-fPIC -O2", "-LDFLAGS", "-shared", "--exe", str(out / "shim.cpp"),
                    "-o", "libsim.so", *(str(SRC / f) for f in sources)],
                   check=True, stdout=subprocess.DEVNULL)
    meta.write_text(json.dumps(ports))
    return lib, ports


class VSim:
    """A Verilator model of `top`: ports by name, eval() and tick()."""

    def __init__(self, sources: list[str], top: str, params: dict | None = None) -> None:
        path, ports = _build(sources, top, params or {})
        self.lib = ctypes.CDLL(str(path))
        self.lib.sim_new.restype = ctypes.c_void_p
        self.lib.sim_eval.argtypes = [ctypes.c_void_p]
        self.lib.sim_get.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.lib.sim_get.restype = ctypes.c_uint64
        self.lib.sim_set.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint64]
        self.ids = {p["name"]: i for i, p in enumerate(ports)}
        self.mask = {p["name"]: (1 << p["bits"]) - 1 for p in ports}
        self.h = self.lib.sim_new()
        self["clk"] = 0
        self.eval()

    def __setitem__(self, name: str, value: int) -> None:
        self.lib.sim_set(self.h, self.ids[name], int(value) & self.mask[name])

    def __getitem__(self, name: str) -> int:
        return self.lib.sim_get(self.h, self.ids[name])

    def eval(self) -> None:
        self.lib.sim_eval(self.h)

    def tick(self) -> None:
        """One rising edge of clk, the logic settled before and after it."""
        self.eval()
        self["clk"] = 1
        self.eval()
        self["clk"] = 0
        self.eval()
