"""chip_lockstep.py - the whole chip core (src/openpio.v: SMs, FIFOs, pins, host registers)
against sim/model/chip.py, every clock, through Verilator (vsim.py).

LockstepChip is a chip.Chip that clocks the RTL beside the model: every clock compares the pads,
HOST_IRQ and the word a host access reads (REG_RD, RX_POP, IMEM_RD), and every few clocks every
register of every SM and of the chip. The IMEMs and FIFO SRAMs are Python lists behind the RTL's
memory ports, with the macros' timing (a read's word a clock later, a write holds the output).
Anything that drives a Chip drives it: host.Host, test/shared/bench.Bench.

The fuzz loads random programs and configurations into both SMs and runs random host traffic
(register writes and reads, FIFO words, IMEM reads, CTRL with EN, RESET and STEP) and pins:

    python3 test/lockstep/chip_lockstep.py [--seed N] [--runs N] [--cycles N]
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sim" / "model"))
from vsim import VSim                                                   # noqa: E402

from asm import IMEM_WORDS, PAU_CODES, AssemblerError, assemble  # noqa: E402
from chip import (CTRL, GPIO, IRQ, OP_IMEM_RD, OP_IMEM_WR, OP_REG_RD, OP_REG_WR, PAU_MODE_BITS,  # noqa: E402
                  OP_RX_POP, OP_TX_PUSH, PADS_BIDIR, TT_PADS, Chip)
from host import Host                                                   # noqa: E402

SOURCES = ["pau.v", "openpio_sm.v", "fifo.v", "openpio.v"]
CHIP_REGS = list(range(0x20, 0x2A))


class Mismatch(AssertionError):
    pass


class LockstepChip(Chip):
    """chip.Chip with the RTL clocked beside it; a difference raises Mismatch."""

    sweep_every = 16                     # clocks between full register comparisons

    def __init__(self, n_sm: int = 2, fifo_depth: int = 4, spi_word_clocks: int = 96,
                 pads: tuple[int, int, int] = TT_PADS) -> None:
        super().__init__(n_sm, fifo_depth, spi_word_clocks, pads)
        assert pads == TT_PADS, "the RTL core is built with the Tiny Tapeout pads"
        self.rtl = VSim(SOURCES, "openpio", {"N_SM": n_sm})
        self.imem = [[0] * IMEM_WORDS for _ in range(n_sm)]
        self.ram = [[0] * 256 for _ in range(n_sm)]
        self.im_out = [0] * n_sm
        self.ram_out = [0] * n_sm
        self.imem_due: int | None = None         # an IMEM_RD's word, due on the RTL next clock
        self.model_only = False
        r = self.rtl
        for k in ("h_valid", "h_op", "h_sm", "h_addr", "h_wdata", "pin_in", "imem_rdata", "ram_rdata"):
            r[k] = 0
        r["rst_n"] = 0
        r.tick()
        r["rst_n"] = 1

    # ------------------------------------------------------------------ the clock

    def step(self, cycles: int = 1) -> None:
        if self.model_only:
            return super().step(cycles)
        for _ in range(cycles):
            self._clock(None)

    def host(self, op: int, n: int, addr: int = 0, word: int = 0) -> int:
        return self._clock((op, n, addr, word))

    def _clock(self, access: tuple[int, int, int, int] | None) -> int:
        r, n_sm = self.rtl, len(self.sms)
        r["pin_in"] = self.pin_in
        r["imem_rdata"] = sum(w << 16 * i for i, w in enumerate(self.im_out))
        r["ram_rdata"] = sum(w << 16 * i for i, w in enumerate(self.ram_out))
        r["h_valid"] = access is not None
        if access is not None:
            op, n, addr, word = access
            r["h_op"], r["h_sm"], r["h_addr"], r["h_wdata"] = op, n, addr, word
        r.eval()
        if self.imem_due is not None:
            self._check("IMEM_RD word", r["h_imem_rdata"], self.imem_due)
            self.imem_due = None
        rtl_read = r["h_rdata"]
        im_addr, im_we, im_wdata = r["imem_addr"], r["imem_we"], r["imem_wdata"]
        ram = [(r["ram_addr"] >> 8 * i & 0xFF, r["ram_we"] >> i & 1, r["ram_re"] >> i & 1,
                r["ram_wdata"] >> 16 * i & 0xFFFF) for i in range(n_sm)]

        if access is None:
            value = None
            super().step()
        else:
            self.model_only = True
            value = super().host(*access)
            self.model_only = False
        r.tick()
        for i in range(n_sm):
            a = im_addr >> 6 * i & 63
            if im_we >> i & 1:
                self.imem[i][a] = im_wdata
            else:
                self.im_out[i] = self.imem[i][a]
            a, we, re_, wd = ram[i]
            if we:
                self.ram[i][a] = wd
            elif re_:
                self.ram_out[i] = self.ram[i][a]

        if access is not None:
            op = access[0]
            if op in (OP_REG_RD, OP_RX_POP):
                self._check(f"host read {access}", rtl_read, value)
            elif op == OP_IMEM_RD:
                self.imem_due = value
        r["h_valid"] = 0
        r.eval()
        out, oe = self.pins
        self._check("pads", r["pin_out"], out)
        self._check("pad enables", r["pin_oe"], oe)
        self._check("HOST_IRQ", r["host_irq"], self.host_irq)
        if self.cycle % self.sweep_every == 0:
            self.compare_registers()
        return value

    # ------------------------------------------------------------------ comparisons

    def peek(self, n: int, addr: int) -> int:
        """Register `addr` of SM n as a REG_RD would read it now, without a clock."""
        r = self.rtl
        r["h_op"], r["h_sm"], r["h_addr"] = OP_REG_RD, n, addr
        r.eval()
        return r["h_rdata"]

    def compare_registers(self) -> None:
        for n in range(len(self.sms)):
            for addr in range(0x20):
                self._check(f"SM {n} register {addr:#04x}", self.peek(n, addr), self.reg_read(n, addr))
        for addr in CHIP_REGS:
            self._check(f"chip register {addr:#04x}", self.peek(0, addr), self.reg_read(0, addr))

    def _check(self, what: str, rtl: int, model: int) -> None:
        if rtl != model:
            raise Mismatch(f"clock {self.cycle}: {what}: rtl={rtl:#x} model={model:#x}")


# ---------------------------------------------------------------------- fuzz

def random_source(rng: random.Random) -> str:
    """A random program in assembler: PAU traffic, FIFOs, peer IRQs, pins, waits."""
    lines = []
    for _ in range(rng.randrange(6, 30)):
        k = rng.random()
        n = rng.randrange(1, 17)
        lines.append(
            f"out pins, {n} auto" if k < .1 else
            f"in pins, {n} auto" if k < .2 else
            f"pull {rng.choice(['block', 'noblock'])}" if k < .27 else
            f"push {rng.choice(['block', 'noblock'])}" if k < .34 else
            f"irq {rng.choice(['', 'clear ', 'wait '])}{rng.choice(['', 'peer '])}{rng.randrange(8)}" if k < .44 else
            f"wait {rng.randrange(2)} {rng.choice(['peer_irq', 'irq', 'gpio', 'pau'])} {rng.randrange(8)}" if k < .5 else
            f"set pau_ctl, {rng.choice(['tx', 'rx', 'tx|rx', 'tx|rx|stuff|crc', 'rx|crc'])}" if k < .56 else
            f"set pins, {rng.randrange(32)}" if k < .62 else
            f"set pindirs, {rng.randrange(32)}" if k < .66 else
            f"mov x, {rng.choice(['status', 'pins', 'isr', 'osr', 'y'])}" if k < .72 else
            f"jmp x--, {rng.randrange(4)}" if k < .78 else
            f"in time, 16 auto" if k < .82 else
            f"out x, {n}" if k < .86 else
            f"set x, {rng.randrange(32)} [{rng.randrange(4)}]" if k < .9 else
            "pau crc_append" if k < .93 else
            f"pau crc_feed {n}" if k < .96 else
            "nop")
    return "\n".join(lines)


def random_load(chip: LockstepChip, host: Host, n: int, rng: random.Random) -> None:
    while True:
        k = rng.random()
        src = ("in time, 16 auto" if k < .15 else        # a word a strobe into the RX FIFO
               "out x, 16 auto" if k < .3 else           # and out of the TX FIFO
               random_source(rng))
        try:
            prog = assemble(src)
            break
        except AssemblerError:                           # more than 4 conditions: again
            pass
    host.load(n, prog, clkdiv_int=rng.choice([1, 1, 2, 3]), clkdiv_frac=rng.choice([0, 0, rng.randrange(256)]),
              in_base=rng.randrange(16), out_base=rng.randrange(16), set_base=rng.randrange(16),
              shift_in_right=rng.random() < .5, shift_out_right=rng.random() < .5,
              push_thresh=rng.randrange(16), pull_thresh=rng.randrange(16),
              pau_mode=rng.randrange(1 << 16) & PAU_MODE_BITS,
              pau_code=rng.choice([*PAU_CODES.values(), rng.randrange(1 << 16)]),
              pau_cfg=rng.randrange(1, 4) | rng.randrange(4) << 5,
              crc_cfg=rng.randrange(128), crc_poly=rng.randrange(1 << 32),
              open_drain_mask=rng.randrange(256) & host.read(0, PADS_BIDIR)[0] if rng.random() < .3 else 0)


def run(seed: int, cycles: int) -> None:
    rng = random.Random(seed)
    chip = LockstepChip(spi_word_clocks=rng.choice([2, 3, 7]))
    host = Host(chip)
    for n in range(len(chip.sms)):
        random_load(chip, host, n, rng)
    host.start(rng.choice([1, 2, 3]))
    loop = rng.random() < .5
    pop = rng.choice([.2, .2, .02])                      # a slow host fills the RX rings
    while chip.cycle < cycles:
        k = rng.random()
        n = rng.randrange(len(chip.sms))
        if k < .25:                                      # bursts fill the SRAM rings
            for _ in range(rng.choice([1, 1, rng.randrange(2, 150)])):
                chip.host(OP_TX_PUSH, n, 0, rng.randrange(1 << 16))
        elif k < .25 + pop:
            for _ in range(rng.choice([1, 1, rng.randrange(2, 150)])):
                chip.host(OP_RX_POP, n)
        elif k < .45:
            chip.step(rng.randrange(1, 8))
        elif k < .5:
            chip.host(OP_REG_RD, n, rng.choice([rng.randrange(0x2A), rng.randrange(0x30, 0x40)]))
        elif k < .53:
            chip.host(OP_REG_WR, n, IRQ, rng.randrange(1 << 16))
        elif k < .55:
            reset = rng.randrange(4) if rng.random() < .3 else 0
            step = rng.randrange(4) if rng.random() < .3 else 0
            chip.host(OP_REG_WR, 0, CTRL, rng.randrange(4) | reset << 4 | step << 8)
        elif k < .56 and not chip.stepping >> n & 1:      # not the port of a stepping SM
            chip.host(OP_IMEM_RD, n, rng.randrange(IMEM_WORDS))
        elif k < .57:
            chip.host(OP_REG_WR, 0, GPIO, rng.randrange(256))
        elif k < .58 and not (chip.en | chip.stepping) >> n & 1:
            addr = rng.randrange(IMEM_WORDS)
            if not (chip.sms[n].stalled and addr == chip.sms[n].pc):   # ISA.md, Constraints
                chip.host(OP_IMEM_WR, n, addr, rng.randrange(1 << 16))
                chip.step()                              # a clock before the SM can fetch it
        else:
            chip.step(rng.randrange(1, 8))
        if rng.random() < .2:
            chip.pin_in ^= 1 << rng.randrange(16)
        if loop:
            chip.pin_in = chip.pin_in & ~0xFF | chip.pins[0] & 0xFF


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--runs", type=int, default=40)
    ap.add_argument("--cycles", type=int, default=4000)
    args = ap.parse_args()
    t0, fails = time.time(), 0
    for i in range(args.runs):
        try:
            run(args.seed * 100_003 + i, args.cycles)
        except Mismatch as e:
            fails += 1
            print(f"run {i} (seed {args.seed}): {e}")
    print(f"{args.runs - fails}/{args.runs} runs agree, {args.cycles} clocks each, {time.time() - t0:.0f} s")
    raise SystemExit(1 if fails else 0)


if __name__ == "__main__":
    main()
