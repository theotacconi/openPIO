"""Gate-level lockstep co-simulation of src/openpio_core.v against sim/model/emu.py.

Yosys synthesizes the RTL to a flat netlist of $_AND_/$_OR_/$_XOR_/$_NOT_/$_MUX_
and $_DFF_P_ cells; the netlist is compiled into straight-line Python and
stepped one clock per emu StateMachine.step().

Yosys: $YOSYS, else `yosys`, else `yowasp-yosys` (pip install yowasp-yosys).
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

SOURCES = ["alu.v", "shifter.v", "imem.v", "pau.v", "openpio_core.v"]


def find_yosys() -> str:
    for cand in (os.environ.get("YOSYS"), "yosys", "yowasp-yosys"):
        if cand and shutil.which(cand):
            return shutil.which(cand)
    raise RuntimeError("Yosys not found: set $YOSYS or pip install yowasp-yosys")


def build(top: str = "openpio_core") -> dict:
    """Lint (check -assert) and synthesize src/ to a gate-level JSON module."""
    with tempfile.TemporaryDirectory() as tmp:
        # yowasp-yosys (WASM) only sees the working directory
        for f in SOURCES:
            shutil.copy(ROOT / "src" / f, tmp)
        script = (f"read_verilog -noautowire {' '.join(SOURCES)}; "
                  f"hierarchy -check -top {top}; proc; check -assert; "
                  f"synth -noabc -flatten -top {top}; dffunmap; opt_clean; write_json net.json")
        subprocess.run([find_yosys(), "-q", "-p", script], cwd=tmp, check=True,
                       stdout=subprocess.DEVNULL)
        return json.load(open(Path(tmp) / "net.json"))["modules"][top]


class Netlist:
    """Compiled two-phase simulator: comb(v) settles logic, clk(v) updates DFFs."""

    def __init__(self, module: dict, xval: int = 0) -> None:
        self.ports = {n: p["bits"] for n, p in module["ports"].items()}
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

    def get(self, name: str) -> int:
        r = 0
        for i, b in enumerate(self.net.pidx[name]):
            r |= (int(b) if isinstance(b, str) else self.v[b]) << i
        return r

    def eval(self) -> None:
        self.net.comb(self.v)

    def clock(self) -> None:
        self.net.comb(self.v)
        self.net.clk(self.v)
        self.net.comb(self.v)


def _bits(flags) -> int:
    return sum(1 << i for i, f in enumerate(flags) if f)


class Lockstep:
    """Bring up a Core from a fresh emu StateMachine, then mirror each step().

    Host-side changes between steps (gpio_in, FIFO contents, cleared IRQ
    flags) are read from the StateMachine and driven onto the core inputs.
    """

    def __init__(self, net: Netlist, sm) -> None:
        self.c = c = Core(net)
        self.sm = sm
        for k in ("en", "imem_wr_en", "imem_wr_addr", "imem_wr_data", "gpio_in", "tx_empty",
                  "tx_data", "rx_full", "irq_clr", "peer_irq_clr"):
            c.set(k, 0)
        cfg = dict(
            cfg_wrap_bottom=sm.wrap_bottom, cfg_wrap_top=sm.wrap_top,
            cfg_ss_count=sm.side_set_count, cfg_ss_opt=sm.side_set_opt,
            cfg_ss_pindirs=sm.side_set_pindirs, cfg_ss_base=sm.sideset_base & 7,
            cfg_in_base=sm.in_base & 7, cfg_out_base=sm.out_base & 7,
            cfg_set_base=sm.set_base & 7, cfg_set_count=sm.set_count,
            cfg_jmp_pin=sm.jmp_pin & 7, cfg_od_mask=sm.open_drain_mask,
            cfg_in_shr=sm.shift_in_right, cfg_out_shr=sm.shift_out_right,
            cfg_clkdiv_int=sm.clkdiv_int, cfg_clkdiv_frac=sm.clkdiv_frac)
        for k, val in cfg.items():
            c.set(k, val)
        c.set("rst_n", 0); c.clock(); c.set("rst_n", 1)
        for a in range(32):
            c.set("imem_wr_en", 1); c.set("imem_wr_addr", a)
            c.set("imem_wr_data", sm.memory[a]); c.clock()
        c.set("imem_wr_en", 0); c.clock()          # en=0 cycle: rd_data <= mem[pc]
        c.set("en", 1)
        self.cycle = 0

    def step(self, emu_step) -> list[str]:
        """Run emu_step(sm) and one core clock; return mismatch descriptions."""
        c, sm = self.c, self.sm
        hw_irq, hw_pirq = c.get("irq_flags"), c.get("peer_irq_flags")
        em_irq, em_pirq = _bits(sm.irq_flags), _bits(sm.peer_irq_flags)
        if (em_irq & ~hw_irq) or (em_pirq & ~hw_pirq):
            raise RuntimeError("host-set IRQ flags have no core input")
        c.set("irq_clr", hw_irq & ~em_irq)
        c.set("peer_irq_clr", hw_pirq & ~em_pirq)
        c.set("gpio_in", sm.gpio_in & 0xFF)
        c.set("tx_empty", not sm.tx_fifo)
        c.set("tx_data", sm.tx_fifo[0] if sm.tx_fifo else 0)
        c.set("rx_full", len(sm.rx_fifo) >= sm.fifo_depth)
        c.eval()
        tx_pop, rx_push, rx_data = c.get("tx_pop"), c.get("rx_push"), c.get("rx_data")
        tx_len, rx_len = len(sm.tx_fifo), len(sm.rx_fifo)

        emu_step(sm)
        c.clock()
        self.cycle += 1

        hp2 = 2 * (sm.pau_baud or 1)
        em_push = len(sm.rx_fifo) > rx_len
        checks = [
            ("tx_pop", tx_pop, int(len(sm.tx_fifo) < tx_len)),
            ("rx_push", rx_push, int(em_push)),
            ("rx_data", rx_data if em_push else 0, sm.rx_fifo[-1] if em_push else 0),
            ("pc", c.get("dbg_pc"), sm.pc),
            ("x", c.get("dbg_x"), sm.x),
            ("y", c.get("dbg_y"), sm.y),
            ("isr", c.get("dbg_isr"), sm.isr),
            ("osr", c.get("dbg_osr"), sm.osr),
            ("isr_cnt", c.get("dbg_isr_cnt"), sm.isr_count),
            ("osr_cnt", c.get("dbg_osr_cnt"), sm.osr_count),
            ("stalled", c.get("dbg_stalled"), int(sm.stalled)),
            ("delay", c.get("dbg_delay"), sm.delay_counter),
            ("gpio_out", c.get("gpio_out"), sm.gpio_out),
            ("pindirs", c.get("gpio_oe"), sm.pindirs),
            ("irq", c.get("irq_flags"), _bits(sm.irq_flags)),
            ("peer_irq", c.get("peer_irq_flags"), _bits(sm.peer_irq_flags)),
            ("tmo_flag", c.get("dbg_timeout_flag"), int(sm.timeout_flag)),
            ("tmo_cnt", c.get("dbg_timeout"), sm.timeout_counter),
            ("tmo_val", c.get("dbg_timeout"), sm.timeout_val),
            ("clkdiv", c.get("dbg_clkdiv"), sm.clkdiv_int),
            ("pau_cfg", c.get("dbg_pau_cfg"), sm.pau_cfg),
            ("pau_baud", c.get("dbg_pau_baud"), sm.pau_baud),
            ("mtx_nsym", c.get("dbg_mtx_nsym"), -(-len(sm.pau_tx_queue) // hp2)),
            ("nrzi_k", c.get("dbg_nrzi_k"), int(sm.pau_nrzi_state == 2)),
            ("nrzi_ones", c.get("dbg_nrzi_ones"), min(sm.pau_ones_count, 7)),
        ]
        return [f"{n}: hw={h:#x} emu={e:#x}" for n, h, e in checks if h != e]
