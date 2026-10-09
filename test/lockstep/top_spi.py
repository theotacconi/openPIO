"""Tiny Tapeout top-level test: bit-banged SPI host on the real pins.

1. Register map: reset defaults, write/read-back of every config register.
2. End-to-end runs: the host programs IMEM and config over SPI, starts the
   SM and keeps pushing TX / popping RX / reading registers / clearing IRQs
   while random pin activity runs. Every enabled clock the embedded core is
   compared with emu.py (configured from what the host wrote), every MISO
   word with an independent model, and pins with the core outputs.

    python3 test/lockstep/top_spi.py [--seed N] [--half H]
"""
import argparse
import collections
import random
import sys

from netlist import ROOT, Core, Netlist, build, compare

from asm import assemble
from emu import StateMachine

TOP = "tt_um_theotacconi_openpio"
OP_IMEM_WR, OP_REG_WR, OP_REG_RD, OP_TX_PUSH, OP_RX_POP, OP_IRQ = 1, 2, 3, 4, 5, 6
REG_MASK = {0: 0x0003, 1: 0x1F1F, 2: 0x071F, 3: 0x7777, 4: 0x0037, 5: 0xFF1F, 6: 0xF8FF}
REG_RESET = {0: 0x0000, 1: 0x1F00, 2: 0x0000, 3: 0x5000, 4: 0x0030, 5: 0x0001, 6: 0x0000}
# core debug ports left open at the top are optimized away: probe their sources
CORE_NET = {"dbg_pau_cfg": "pau_cfg_r", "dbg_pau_baud": "pau_baud_r", "dbg_clkdiv": "clkdiv",
            "dbg_timeout": "tmo_cnt", "dbg_delay": "delay_cnt", "dbg_isr_cnt": "isr_cnt",
            "dbg_osr_cnt": "osr_cnt", "dbg_mtx_nsym": "u_pau.m_nsym", "dbg_nrzi_k": "u_pau.nrzi_k",
            "dbg_nrzi_ones": "u_pau.ones"}

ap = argparse.ArgumentParser()
ap.add_argument("--seed", type=int, default=1)
ap.add_argument("--half", type=int, default=6, help="SCLK half-period in clk cycles")
ap.add_argument("--cycles", type=int, default=6000, help="clocks per end-to-end run")
args = ap.parse_args()

net = Netlist(build(TOP))
rng = random.Random(args.seed)
emu_step = StateMachine.step


def core_get(c: Core, name: str, sm) -> int:
    if name == "dbg_pau_cfg":
        # PAU_CFG bits 2/4 feed no logic; their FFs are removed at the top
        return (c.get("u_core.pau_cfg_r", 0) & 0b01011) | (sm.pau_cfg & 0b10100)
    return c.get("u_core." + CORE_NET.get(name, name))


class Top:
    def __init__(self) -> None:
        self.c = Core(net)
        self.ui = 0b0000_0100          # CS_n high
        self.uio = 0
        self.frames: collections.deque = collections.deque()   # pending (cmd, data, callback)
        self.wave: collections.deque = collections.deque()
        self.miso: list = []
        self.sm = None
        self.errors: list = []
        self.pad_hist = collections.deque([0, 0, 0], maxlen=3)
        self.cmd_expect = None
        self.stats: collections.Counter = collections.Counter()
        self.irq_strobe = (0, 0)
        self.c.set("ena", 1)
        self.c.set("rst_n", 0); self.tick(); self.c.set("rst_n", 1)

    # ---------------------------------------------------------------- SPI
    def spi(self, cmd: int, data: int = 0, done=None) -> None:
        self.frames.append((cmd, data, done))

    def _load_frame(self) -> None:
        cmd, data, done = self.frames.popleft()
        h, word = args.half, (cmd << 16) | data
        w = [(1, 0, 0, None)] * h + [(0, 0, 0, None)] * h      # CS_n falls
        for i in range(23, -1, -1):
            b = (word >> i) & 1
            w += [(0, 0, b, None)] * (h - 1) + [(0, 0, b, "sample")]
            w += [(0, 1, b, None)] * h
        w += [(0, 0, 0, None)] * h + [(1, 0, 0, ("done", cmd, data, done))]
        self.wave.extend(w)
        self.miso = []

    def busy(self) -> bool:
        return bool(self.frames or self.wave)

    # --------------------------------------------------------------- clock
    def tick(self) -> None:
        c = self.c
        if not self.wave and self.frames:
            self._load_frame()
        ev = None
        if self.wave:
            cs_n, sclk, mosi, ev = self.wave.popleft()
            self.ui = (self.ui & ~0b111) | (cs_n << 2) | (mosi << 1) | sclk
        c.set("ui_in", self.ui)
        c.set("uio_in", self.uio)
        c.eval()
        if ev == "sample":
            self.miso.append(c.get("uo_out") & 1)

        # pins / glue invariants
        if c.get("uio_out") != c.get("u_core.gpio_out") or c.get("uio_oe") != c.get("u_core.gpio_oe"):
            self.errors.append("uio mapping")
        uo = c.get("uo_out")
        if (uo >> 2) != (c.get("u_core.gpio_out") >> 2) or ((uo >> 1) & 1) != int(c.get("u_core.irq_flags") != 0):
            self.errors.append("uo_out mapping")
        sel = c.get("u_spi.cfg_in_sel") << 3
        self.pad_hist.append((self.ui & sel) | (self.uio & ~sel & 0xFF))
        if c.get("u_core.gpio_in") != self.pad_hist[0]:
            self.errors.append(f"gpio sync: core sees {c.get('u_core.gpio_in'):#x}, pad 2 clk ago {self.pad_hist[0]:#x}")

        if c.get("u_spi.irq_wr"):
            self.irq_strobe = (c.get("u_core.irq_clr"), c.get("u_core.peer_irq_clr"))
        if c.get("u_spi.cmd_end"):
            self.cmd_expect = self.expected_response(c.get("u_spi.cmd_rx"))

        lock = self.sm is not None and c.get("u_core.en")
        if lock:
            sm = self.sm
            sm.gpio_in = c.get("u_core.gpio_in")
            for i in range(8):
                if (c.get("u_core.irq_clr") >> i) & 1: sm.irq_flags[i] = False
                if (c.get("u_core.peer_irq_clr") >> i) & 1: sm.peer_irq_flags[i] = False
            sm.tx_fifo = collections.deque([] if c.get("u_core.tx_empty") else [c.get("u_core.tx_data")], maxlen=4)
            sm.rx_fifo = collections.deque([0] * 4 if c.get("u_core.rx_full") else [], maxlen=4)
            pre = (c.get("u_core.tx_pop"), c.get("u_core.rx_push"), c.get("u_core.rx_data"),
                   len(sm.tx_fifo), len(sm.rx_fifo))
            if pre[0]: self.tx_model_pop(c.get("u_core.tx_data"))
            if pre[1]: self.rx_model.append(c.get("u_core.rx_data"))
            emu_step(sm)
        c.clock()
        if lock:
            errs = compare(lambda n: core_get(c, n, sm), sm, *pre)
            if errs:
                self.errors.append(f"core lockstep: {errs}")

        if isinstance(ev, tuple):
            _, cmd, data, done = ev
            self.stats[["nop", "imem_wr", "reg_wr", "reg_rd", "tx_push", "rx_pop", "irq", "nop"][cmd >> 5]] += 1
            resp = 0
            for b in self.miso[8:]:
                resp = (resp << 1) | b
            if self.cmd_expect is not None and resp != self.cmd_expect:
                self.errors.append(f"MISO cmd {cmd:#04x}: got {resp:#06x} expected {self.cmd_expect:#06x}")
            self.cmd_expect = None
            if cmd >> 5 == OP_IRQ:
                if self.irq_strobe != (data & 0xFF, data >> 8):
                    self.errors.append(f"IRQ W1C data {data:#06x}: core got clr={self.irq_strobe}")
                self.irq_strobe = (0, 0)
            if done:
                done(resp)

    # ------------------------------------------------- host-side model
    def reset_models(self) -> None:
        self.tx_model: collections.deque = collections.deque()
        self.rx_model: collections.deque = collections.deque()
        self.regs = dict(REG_RESET)

    def tx_model_pop(self, word: int) -> None:
        self.stats["sm_pulled"] += 1
        exp = self.tx_model.popleft() if self.tx_model else None
        if exp != word:
            self.errors.append(f"TX FIFO order: core popped {word:#06x}, host pushed {exp}")

    def expected_response(self, cmd: int) -> int:
        c, op, addr = self.c, cmd >> 5, cmd & 31
        status = (c.get("tx_empty") | c.get("tx_full") << 1 | c.get("rx_empty") << 2 |
                  c.get("rx_full") << 3 | c.get("sm_stalled") << 4 | c.get("sm_timeout_flag") << 5 |
                  c.get("sm_pc") << 8)
        if op == OP_REG_RD:
            ro = {8: status, 9: c.get("sm_x"), 10: c.get("sm_y"), 11: c.get("sm_isr"),
                  12: c.get("sm_osr"), 13: c.get("gpio_oe") << 8 | c.get("gpio_out")}
            return self.regs.get(addr, ro.get(addr, 0))
        if op == OP_RX_POP:
            self.stats["rx_words"] += bool(self.rx_model)
            return self.rx_model.popleft() if self.rx_model else 0
        if op == OP_IRQ:
            return c.get("peer_irq_flags") << 8 | c.get("irq_flags")
        return status

    def write_reg(self, addr: int, val: int) -> None:
        def written(_):
            if addr in REG_MASK:
                self.regs[addr] = val & REG_MASK[addr]
        self.spi(OP_REG_WR << 5 | addr, val, written)

    def run(self, clocks: int) -> None:
        for _ in range(clocks):
            self.tick()

    def drain(self) -> None:
        while self.busy():
            self.tick()


def check(top: Top, name: str) -> int:
    if top.errors:
        print(f"FAIL {name}: {len(top.errors)} errors, first: {top.errors[:3]}")
        return 1
    st = top.stats
    sm = top.sm
    run = (f"  [{sm.cycle_count} SM clk; frames: " + " ".join(f"{k}={st[k]}" for k in
           ("imem_wr", "reg_wr", "reg_rd", "tx_push", "rx_pop", "irq")) +
           f"; SM pulled {st['sm_pulled']}, host got {st['rx_words']} RX words]") if sm else ""
    print(f"ok   {name}{run}")
    return 0


fails = 0

# ---------------------------------------------------------------- 1. registers
top = Top()
top.reset_models()
for addr in range(16):
    top.spi(OP_REG_RD << 5 | addr)
for _ in range(3):
    for addr in range(7):
        top.write_reg(addr, rng.randrange(1 << 16) & ~0x0003 if addr == 0 else rng.randrange(1 << 16))
    for addr in range(16):
        top.spi(OP_REG_RD << 5 | addr)
top.drain()
fails += check(top, f"register map (SCLK half-period {args.half} clk)")


# ------------------------------------------------------------- 2. end-to-end
def end_to_end(name: str, prog, kw: dict, shr=(True, True)) -> None:
    global fails
    top = Top()
    top.reset_models()
    sm = StateMachine(prog, **kw)
    sm.shift_in_right, sm.shift_out_right = shr
    for a in range(32):
        top.spi(OP_IMEM_WR << 5 | a, sm.memory[a])
    in_sel = rng.choice([0, 0, rng.randrange(32)])
    top.write_reg(1, sm.wrap_top << 8 | sm.wrap_bottom)
    top.write_reg(2, sm.sideset_base << 8 | sm.side_set_pindirs << 4 | sm.side_set_opt << 3 | sm.side_set_count)
    top.write_reg(3, sm.set_count << 12 | sm.set_base << 8 | sm.out_base << 4 | sm.in_base)
    top.write_reg(4, sm.shift_out_right << 5 | sm.shift_in_right << 4 | sm.jmp_pin)
    top.write_reg(5, sm.clkdiv_frac << 8 | sm.clkdiv_int)
    top.write_reg(6, in_sel << 11 | sm.open_drain_mask)
    top.write_reg(0, 0b10)            # RESET: loads CLKDIV, flushes FIFOs
    top.write_reg(0, 0b01)            # EN
    top.sm = sm                       # emu steps from the core's first en=1 clock
    top.drain()

    status = 0
    def got_status(r):
        nonlocal status
        status = r
    for _ in range(args.cycles):
        if rng.random() < .1:
            top.uio ^= 1 << rng.randrange(8)
        if rng.random() < .05:
            top.ui ^= 1 << rng.randrange(3, 8)
        if not top.busy():
            act = rng.random()
            if act < .25:
                top.spi(OP_REG_RD << 5 | 8, done=got_status)
            elif act < .55 and not status & 2:              # TX not full at last status
                w = rng.randrange(1 << 16)
                top.spi(OP_TX_PUSH << 5, w)
                top.tx_model.append(w)
                status |= 2                                   # re-read before next push
            elif act < .8 and not status & 4:               # RX not empty at last status
                top.spi(OP_RX_POP << 5)
                status |= 4
            elif act < .9:
                top.spi(OP_IRQ << 5, rng.choice([0, 0x0001, 0x00FF, 0xFFFF, rng.randrange(1 << 16)]))
            else:
                top.spi(OP_REG_RD << 5 | rng.randrange(9, 14))
        top.tick()
    top.drain()
    fails += check(top, name)


# FIFO ordering, full/drop and back-pressure: echo every TX word to RX.
echo = assemble("""
.wrap_target
    pull block
    mov isr, osr
    push block
.wrap
""")
top = Top()
top.reset_models()
sm = StateMachine(echo)
for a in range(32):
    top.spi(OP_IMEM_WR << 5 | a, sm.memory[a])
top.write_reg(1, sm.wrap_top << 8 | sm.wrap_bottom)
top.write_reg(0, 0b10)
top.write_reg(0, 0b00)
words = [rng.randrange(1 << 16) for _ in range(12)]
for w in words[:4]:                       # EN=0: TX fills to 4
    top.spi(OP_TX_PUSH << 5, w)
    top.tx_model.append(w)
top.spi(OP_TX_PUSH << 5, 0xDEAD)          # 5th push while full: dropped
st = []
top.spi(OP_REG_RD << 5 | 8, done=st.append)
top.drain()
if not st or (st[0] & 0b1111) != 0b0110:
    top.errors.append(f"status after 4+1 pushes: {st}")
top.write_reg(0, 0b01)
top.sm = sm
top.drain()
top.run(200)                              # SM echoes 4 words; RX full; 5th push would stall
for w in words[4:8]:
    top.spi(OP_TX_PUSH << 5, w)
    top.tx_model.append(w)
top.run(300)
got = []
for _ in range(8):
    top.spi(OP_RX_POP << 5, done=got.append)
    top.run(rng.randrange(0, 40))
for w in words[8:]:
    top.spi(OP_TX_PUSH << 5, w)
    top.tx_model.append(w)
top.run(200)
for _ in range(4):
    top.spi(OP_RX_POP << 5, done=got.append)
top.drain()
if got != words:
    top.errors.append(f"echo order: sent {[hex(w) for w in words]} got {[hex(w) for w in got]}")
fails += check(top, "FIFO echo: order, full drop, push back-pressure")

for agent, kw, shr in [
    ("uart/uart.asm", dict(in_base=0, jmp_pin=0), (True, True)),
    ("spi_mode0/spi_mode0.asm", dict(sideset_base=0, out_base=1, in_base=2), (False, False)),
    ("i2c/i2c.asm", dict(set_base=0, out_base=0, in_base=0, jmp_pin=1), (False, False)),
    ("onewire/onewire.asm", dict(set_base=0, out_base=0, in_base=0, jmp_pin=0), (True, True)),
    ("manchester/manchester.asm", dict(set_base=0, out_base=0), (True, False)),
    ("usb_ls/usb_ls.asm", dict(set_base=0, out_base=0), (True, True)),
]:
    end_to_end(f"end-to-end {agent}", assemble((ROOT / "agents" / agent).read_text()), kw, shr)

for i in range(4):
    n = rng.randrange(6, 33)
    prog = [rng.randrange(1 << 16) for _ in range(n)]
    prog = [w if (w >> 13) != 7 or ((w >> 5) & 7) != 5 else w & ~0x1F for w in prog]  # fixed PAU_BAUD
    kw = dict(side_set_count=rng.choice([0, 1, 2]), clkdiv_int=rng.choice([1, 2]),
              clkdiv_frac=rng.choice([0, 77]), in_base=rng.randrange(8), out_base=rng.randrange(8),
              set_base=rng.randrange(8), set_count=rng.randrange(6), sideset_base=rng.randrange(8),
              jmp_pin=rng.randrange(8), open_drain_mask=rng.choice([0, 0x5A]))
    sm0 = StateMachine(prog, **kw)
    end_to_end(f"end-to-end random program {i}", prog, kw, (rng.random() < .5, rng.random() < .5))

print(f"{fails} failing")
sys.exit(1 if fails else 0)
