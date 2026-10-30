"""Random-program lockstep of one state machine: src/openpio_sm.v (with src/pau.v) against
sim/model/emu.py's StateMachine, compared every clock.

Random configurations (written through the host registers, as HOST.md) and programs: 256
random instruction words (four pages), or (half of them) a loop of PAU work (OUT and IN through the PAU with
autopull and autopush, CRC append and feed, PAU_CTL changes, waits on PAU status) with the TX pin
looped back to the RX pin, so the line codes, stuffing, monitor and CRC run on real traffic.
Between clocks random input pins (sometimes the PAU TX pin looped back to
its RX pin), TX FIFO words, RX FIFO reads, the peer's IRQ flags, host IRQ writes. Each clock
compares the FIFO handshakes, the pin writes, the peer flag writes and the state; every few
clocks every host register.

    python3 test/lockstep/sm_fuzz.py [--seed N] [--programs N] [--cycles N]
"""
from __future__ import annotations

import argparse
import random
import time

from netlist import Core, Netlist, build

import chip
from chip import (CLKDIV, CLKDIV_FRAC, COND_CFG, CRC_CFG, CRC_POLY_HI, CRC_POLY_LO, CRC_RES_HI,
                  CRC_RES_LO, FIFO_LEVEL, IRQ, IRQ_MASK, MISC, PAU_CFG, PAU_CODE, PAU_MODE, PIN_MASK,
                  PINBASE, SIDESET, SYNC_HI, SYNC_LO, WRAP, sm_register)
from asm import IMEM_WORDS, PAU_CODES, PAU_STATUS_NAMES, assemble
from emu import StateMachine

SOURCES = ["pau.v", "openpio_sm.v"]
CONFIG_REGS = [WRAP, SIDESET, PINBASE, PIN_MASK, MISC, CLKDIV, CLKDIV_FRAC, PAU_MODE, CRC_CFG,
               CRC_POLY_LO, CRC_POLY_HI, CRC_RES_LO, CRC_RES_HI, IRQ_MASK, PAU_CODE, SYNC_LO,
               SYNC_HI, PAU_CFG]
FIFO_DEPTH = 4


def bits(flags) -> int:
    return sum(1 << i for i, f in enumerate(flags) if f)


def random_word(rng: random.Random) -> int:
    op = rng.choices(range(8), weights=[3, 2, 3, 3, 3, 2, 4, 4])[0]
    w = op << 13 | rng.randrange(1 << 13)
    if rng.random() < .6:
        w &= ~(0x1F << 8)                               # no delay / side-set
    if op == 6 and w >> 6 & 3 == 3 and rng.random() < .5:
        w &= ~0xF if w >> 4 & 3 != 3 else ~0            # mostly valid PAU actions
    if op == 7 and w >> 5 & 7 == 6:
        w = w & ~0x1F | rng.randrange(1, 4)             # SET PAU_BAUD: fast
    if op == 7 and w >> 5 & 7 == 4 and rng.random() < .8:
        w = w & ~0x1F | rng.randrange(3)                # SET DLYMUL: small
    return w


def pau_program(rng: random.Random) -> list[int]:
    """A loop of PAU traffic: TX and RX on, data from the TX FIFO, received bits to the RX FIFO."""
    def flags(tx_rx: bool) -> str:
        f = [k for k in ("stuff", "crc", "monitor") if rng.random() < .5]
        return "|".join((["tx", "rx"] if tx_rx or rng.random() < .7 else [rng.choice(["tx", "rx"])]) + f)
    body = [f"out pins, {rng.choice([8, 16])} auto"]          # traffic first: RX waits for it
    for _ in range(rng.randrange(4, 16)):
        k = rng.random()
        n = rng.choice([1, 2, 3, 5, 8, 16, rng.randrange(1, 17)])
        dly = f" [{rng.randrange(4)}]" if rng.random() < .2 else ""
        body.append(
            f"out pins, {n} auto{dly}" if k < .3 else
            f"in pins, {rng.randrange(1, 5)} auto{dly}" if k < .55 else
            "pau crc_append" if k < .62 else
            f"pau crc_feed {n}" if k < .67 else
            "pau crc_reset" if k < .72 else
            f"set pau_ctl, {flags(False)}" if k < .8 else
            f"set pau_baud, {rng.randrange(1, 5)}" if k < .83 else
            f"wait 1 pau {rng.choice(['tx_idle', 'tx_ready', 'rx_ready'])}" if k < .86 else
            f"jmp pau {rng.choice(list(PAU_STATUS_NAMES))}, 1" if k < .88 else
            "pau tx_abort" if k < .9 else
            "mov x, status\n    mov isr, x" if k < .95 else
            f"nop [{rng.randrange(1, 8)}]")
    src = f"set pau_ctl, {flags(True)}\n.wrap_target\n    " + "\n    ".join(body) + "\n.wrap\n"
    words = list(assemble(src))
    return words + [0] * (IMEM_WORDS - len(words))


def random_sm(rng: random.Random) -> tuple[StateMachine, int]:
    ss = rng.choice([0, 0, 1, 2, 3])
    opt = ss > 0 and ss < 5 and rng.random() < .4
    width = rng.choice([1, 5, 8, 15, 16, 32, rng.randrange(1, 33)])
    pau = rng.random() < .5
    words = pau_program(rng) if pau else [random_word(rng) for _ in range(IMEM_WORDS)]
    sm = StateMachine(
        words, fifo_depth=FIFO_DEPTH,
        side_set_count=ss, side_set_opt=opt, side_set_pindirs=rng.random() < .2,
        clkdiv_int=rng.choice([1, 1, 1, 2, 3]), clkdiv_frac=rng.choice([0, 0, 0, rng.randrange(256)]),
        in_base=rng.randrange(16), out_base=rng.randrange(16), set_base=rng.randrange(16),
        set_count=rng.randrange(8), sideset_base=rng.randrange(16),
        cond_cfg=[rng.randrange(256) for _ in range(4)],
        push_thresh=rng.randrange(16), pull_thresh=rng.randrange(16),
        pau_mode=rng.randrange(1 << 16) & chip.PAU_MODE_BITS,
        pau_code=rng.choice([*PAU_CODES.values(), rng.randrange(1 << 16)]),
        pau_cfg=rng.randrange(1, 4) | rng.randrange(4) << 5
        | rng.choice([0, 0, rng.randrange(1, 9), rng.randrange(33)]) << 8,
        sync=rng.randrange(1 << 32),
        crc_cfg=(width - 1) | rng.randrange(4) << 5, crc_poly=rng.randrange(1 << 32),
        crc_res=rng.randrange(1 << 32), pin_mask=rng.choice([0xFFFF, rng.randrange(1 << 16)]))
    if pau:
        sm.pau_mode = sm.pau_mode & ~0xF00 | rng.randrange(1, 7) << 8     # stuffing often
        sm.wrap_bottom, sm.wrap_top = 1, max(i for i, w in enumerate(words) if w)
        sm.side_set_count = 0
        sm.cond_cfg = list(chip.Chip().sms[0].cond_cfg)
    else:
        n = rng.randrange(8, IMEM_WORDS + 1)
        sm.wrap_bottom = rng.randrange(n)
        sm.wrap_top = rng.randrange(sm.wrap_bottom, n)
    sm.shift_in_right, sm.shift_out_right = rng.random() < .5, rng.random() < .5
    sm.reset()
    return sm, rng.choice([0xFF, rng.randrange(1 << 10)])


class Bench:
    """The RTL SM with the IMEM, the pin latches and the FIFO fronts of the model around it."""

    def __init__(self, net: Netlist, sm: StateMachine, irq_mask: int) -> None:
        self.c = c = Core(net)
        self.sm, self.irq_mask = sm, irq_mask
        self.instr = 0
        for k in ("en", "run", "reset", "h_we", "h_addr", "h_wdata", "gpio_in", "latch_out",
                  "latch_dir", "tx_empty", "tx_data", "rx_full", "tx_fifo_empty", "rx_fifo_full",
                  "peer_irq", "in_we", "in_idx", "in_val"):
            c.set(k, 0)
        c.set("rst_n", 0)
        self.clock()
        c.set("rst_n", 1)
        writes = [(a, sm_register(sm, a, irq_mask)) for a in CONFIG_REGS]
        writes += [(COND_CFG, s << 8 | b) for s, b in enumerate(sm.cond_cfg)]
        for addr, word in writes:
            self.host(addr, word)
            self.clock()
        self.host(None)
        c.set("reset", 1)                                # CTRL.RESET: the model's reset state
        self.clock()
        c.set("reset", 0)
        c.set("en", 1)
        c.set("run", 1)

    def host(self, addr: int | None, word: int = 0) -> None:
        self.c.set("h_we", addr is not None)
        self.c.set("h_addr", addr or 0)
        self.c.set("h_wdata", word)

    def clock(self) -> None:
        self.c.set("instr", self.instr)
        self.c.eval()
        addr = self.c.get("imem_addr")
        self.c.clock()
        self.instr = self.sm.memory[addr]

    def inputs(self) -> None:
        """The inputs from the model's state now: the start of the next clock."""
        c, sm = self.c, self.sm
        c.set("gpio_in", sm.gpio_in)
        c.set("latch_out", sm.bus.out)
        c.set("latch_dir", sm.bus.dirs)
        c.set("tx_empty", not sm.tx_fifo)
        c.set("tx_data", sm.tx_fifo[0] if sm.tx_fifo else 0)
        c.set("rx_full", len(sm.rx_fifo) >= FIFO_DEPTH)
        c.set("tx_fifo_empty", sm.tx_level == 0)
        c.set("rx_fifo_full", sm.rx_level >= sm.capacity)
        c.set("peer_irq", bits(sm.peer_irq_flags))

    def read(self, addr: int) -> int:
        self.c.set("h_addr", addr)
        self.c.eval()
        return self.c.get("h_rdata")


def compare_state(c: Core, sm: StateMachine) -> list[str]:
    g = c.get
    checks = [
        ("pc", g("pc"), sm.pc), ("x", g("x"), sm.x), ("y", g("y"), sm.y),
        ("isr", g("isr"), sm.isr), ("osr", g("osr"), sm.osr),
        ("isr_cnt", g("isr_cnt"), sm.isr_count), ("osr_cnt", g("osr_cnt"), sm.osr_count),
        ("stalled", g("stalled"), int(sm.stalled)), ("irq", g("irq"), bits(sm.irq_flags)),
        ("delaying", g("delaying"), int(sm.delay_counter > 0)),
        ("tmo_flag", g("tmo_flag"), int(sm.timeout_flag)), ("dlymul", g("dlymul"), sm.dlymul),
        ("tmo_val", g("tmo_val"), sm.timeout_val), ("time", g("time_r"), sm.time),
        ("cond", g("cond"), sum(v << 8 * i for i, v in enumerate(sm.cond))),
        ("pau_ctl", g("u_pau.ctl"), sm.pau_ctl), ("pau_baud", g("u_pau.baud"), sm.pau_baud),
        ("tx_queue", g("u_pau.tq_len"), len(sm.tx_queue)),
        ("rx_queue", g("u_pau.rq_len"), len(sm.rx_queue)),
        ("crc", g("u_pau.crc_a"), sm.crc),
    ]
    if sm.cond_prev is not None:
        checks.append(("cond_prev", g("cond_prev"), bits(sm.cond_prev)))
    if sm.delay_counter:
        checks.append(("delay", g("cnt"), sm.delay_counter))
    elif sm.stalled and sm.stall_reason == "WAIT" and sm.stall_info["t_en"]:
        checks.append(("timeout", g("cnt"), sm.timeout_counter))
    return [f"{n}: rtl={h:#x} model={e:#x}" for n, h, e in checks if h != e]


def run_program(net: Netlist, rng: random.Random, cycles: int, verbose: bool) -> list[str]:
    sm, irq_mask = random_sm(rng)
    b = Bench(net, sm, irq_mask)
    c = b.c
    loop = sm.wrap_bottom == 1 or rng.random() < .3
    regs = [a for a in [*range(1, 32), *range(0x30, 0x34)] if a != FIFO_LEVEL]
    for cyc in range(cycles):
        b.inputs()
        irq_w = rng.randrange(1 << 16) if rng.random() < .03 else None
        b.host(IRQ if irq_w is not None else None, irq_w or 0)
        c.set("instr", b.instr)
        c.eval()
        out = {k: c.get(k) for k in ("tx_pop", "rx_push", "rx_data", "out_we", "out_val", "dir_we",
                                     "dir_val", "peer_we", "peer_idx", "peer_val", "strobe",
                                     "imem_addr")}
        tx_len, rx_len = len(sm.tx_fifo), len(sm.rx_fifo)
        latch, dirs, peer = sm.bus.out, sm.bus.dirs, bits(sm.peer_irq_flags)

        sm.step()
        if irq_w is not None:                            # lands at the end of the clock
            for i in range(8):
                if irq_w >> i & 1:
                    sm.irq_flags[i] = False
                if irq_w >> (8 + i) & 1:
                    sm.irq_flags[i] = True
        c.clock()
        b.instr = sm.memory[out["imem_addr"]]

        pushed = len(sm.rx_fifo) > rx_len
        peer_rtl = peer
        if out["peer_we"]:
            peer_rtl = peer & ~(1 << out["peer_idx"]) | out["peer_val"] << out["peer_idx"]
        pau_m, pau_v = c.get("pau_we"), c.get("pau_val")
        errs = [f"{n}: rtl={h:#x} model={e:#x}" for n, h, e in [
            ("strobe", out["strobe"], int(sm.strobed)),
            ("tx_pop", out["tx_pop"], int(len(sm.tx_fifo) < tx_len)),
            ("rx_push", out["rx_push"], int(pushed)),
            ("rx_data", out["rx_data"] if pushed else 0, sm.rx_fifo[-1] if pushed else 0),
            ("gpio_out", latch & ~out["out_we"] | out["out_val"] & out["out_we"], sm.bus.out),
            ("pindirs", dirs & ~out["dir_we"] | out["dir_val"] & out["dir_we"], sm.bus.dirs),
            ("peer_irq", peer_rtl, bits(sm.peer_irq_flags)),
            ("pins", sm.bus.out & ~pau_m | pau_v & pau_m, sm.pau_override(sm.bus.out)),
        ] if h != e]
        errs += compare_state(c, sm)
        if cyc % 8 == 0 or errs:
            b.inputs()
            errs += [f"reg {a:#04x}: rtl={b.read(a):#06x} model={sm_register(sm, a, irq_mask):#06x}"
                     for a in regs if b.read(a) != sm_register(sm, a, irq_mask)]
        if errs:
            if verbose:
                print("\n".join(sm.history))
            return [f"cycle {cyc}: {e}" for e in errs]

        # the world between clocks
        if rng.random() < .3:
            sm.gpio_in ^= 1 << rng.randrange(16)
        if loop and rng.random() < .9:
            pins = sm.pau_override(sm.bus.out)
            sm.gpio_in = sm.gpio_in & ~(1 << sm.in_base) | (pins >> sm.out_base & 1) << sm.in_base
        if rng.random() < .3 and len(sm.tx_fifo) < FIFO_DEPTH:
            sm.tx_fifo.append(rng.randrange(1 << 16))
        if rng.random() < .2 and sm.rx_fifo:
            sm.rx_fifo.popleft()
        if rng.random() < .03:
            sm.peer_irq_flags[rng.randrange(8)] ^= True
    return []


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--programs", type=int, default=50)
    ap.add_argument("--cycles", type=int, default=300)
    ap.add_argument("-v", "--verbose", action="store_true", help="print the model's trace on a mismatch")
    args = ap.parse_args()
    t0 = time.time()
    net = Netlist(build(SOURCES, "openpio_sm"))
    print(f"netlist built in {time.time() - t0:.1f} s")
    fails = 0
    for p in range(args.programs):
        rng = random.Random(args.seed * 100_003 + p)
        errs = run_program(net, rng, args.cycles, args.verbose)
        if errs:
            fails += 1
            print(f"program {p} (seed {args.seed}):", *errs[:8], sep="\n  ")
    print(f"{args.programs - fails}/{args.programs} programs agree, {args.cycles} clocks each, "
          f"{time.time() - t0:.0f} s")
    raise SystemExit(1 if fails else 0)


if __name__ == "__main__":
    main()
