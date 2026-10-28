"""top_lockstep.py - the Tiny Tapeout top (src/tt_um_theotacconi_openpio.v: SPI slave, chip
core, memory models) driven through its pads by a bit-banged SPI master, against chip.py.

Every clock the model makes the host access the RTL's SPI slave makes (top_tb.v brings it out),
and the pads are compared; every MISO word must be what the model reads: the STATUS of the
previous transaction's SM during the header, the read words, 0 for RX_POP words beyond its
count. host.Host loads and runs random programs through it, with random SPI traffic.

    python3 test/lockstep/top_lockstep.py [--seed N] [--runs N] [--cycles N]
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "sim" / "model"))
from vsim import VSim                                                    # noqa: E402

from chip import (OP_IMEM_RD, OP_REG_RD, OP_RX_POP, OP_TX_PUSH, Chip, header)  # noqa: E402
from chip_lockstep import Mismatch, random_load                          # noqa: E402
from host import Host                                                    # noqa: E402

SOURCES = ["spi.v", "pau.v", "openpio_sm.v", "fifo.v", "openpio.v", "imem.v", "fifo_ram.v",
           "tt_um_theotacconi_openpio.v", str(HERE / "top_tb.v")]
HALF = 3                                   # clocks of each SCLK phase (SCLK = clk / 6)


class SpiTop:
    """The RTL top and the model clocked together; spi() is a transaction on the pads, so
    host.Host drives it as it drives a Chip."""

    def __init__(self) -> None:
        self.rtl = VSim(SOURCES, "top_tb")
        self.chip = Chip(2)
        self.sms = self.chip.sms
        self.pin_in = 0                                  # input pads, bit = pin
        self.cs = self.sclk = self.mosi = 0
        self.cs = 1
        self.last_sm = 0
        r = self.rtl
        r["ui_in"] = self._ui()
        r["uio_in"] = 0
        r["rst_n"] = 0
        r.tick()
        r["rst_n"] = 1

    @property
    def cycle(self) -> int:
        return self.chip.cycle

    def _ui(self) -> int:
        return (self.pin_in >> 11 & 0x1F) << 3 | self.cs << 2 | self.mosi << 1 | self.sclk

    def step(self, cycles: int = 1) -> list[int]:
        """Clock both; returns the words the model read for the RTL's read accesses."""
        reads = []
        r, chip = self.rtl, self.chip
        for _ in range(cycles):
            r["ui_in"] = self._ui()
            r["uio_in"] = self.pin_in & 0xFF
            r.eval()
            chip.pin_in = self.pin_in
            if r["h_valid"]:
                op = r["h_op"]
                value = chip.host(op, min(r["h_sm"], len(chip.sms) - 1), r["h_addr"], r["h_wdata"])
                if op in (OP_REG_RD, OP_RX_POP, OP_IMEM_RD):
                    reads.append(value)
            else:
                chip.step()
            r.tick()
            out, oe = chip.pins
            uo = r["uo_out"]
            self._check("uo_out pins", uo >> 2, out >> 10 & 0x3F)
            self._check("HOST_IRQ", uo >> 1 & 1, chip.host_irq)
            self._check("uio_out", r["uio_out"], out & 0xFF)
            self._check("uio_oe", r["uio_oe"], oe & 0xFF)
        return reads

    def spi(self, hdr: int, words=()) -> list[int]:
        """One transaction, SCLK = clk / 6, mode 0: MISO sampled at each rising edge."""
        op, n, addr = hdr >> 13 & 7, hdr >> 11 & 3, hdr & 0x3F
        n = min(n, len(self.sms) - 1)
        self.cs = 0
        reads = self.step(2)                             # CS_N through the synchronizer
        expect = [self.chip._sm_status_reg(self.sms[self.last_sm])]
        self.last_sm = n
        reads += self.step(HALF - 2)
        miso = []
        for word in [hdr, *words]:
            got = 0
            for bit in range(15, -1, -1):
                self.mosi = word >> bit & 1
                reads += self.step(HALF)
                self.sclk = 1
                got = got << 1 | self.rtl["uo_out"] & 1
                reads += self.step(HALF)
                self.sclk = 0
            miso.append(got)
        self.cs = 1
        reads += self.step(2 * HALF)
        if op in (OP_REG_RD, OP_IMEM_RD):
            expect += reads[:len(words)]
        elif op == OP_RX_POP:
            pops = addr or 64
            expect += reads[:min(pops, len(words))] + [0] * max(0, len(words) - pops)
        for k, (got, want) in enumerate(zip(miso, expect)):
            self._check(f"MISO word {k} of {hdr:#06x}", got, want)
        return miso

    def _check(self, what: str, rtl: int, model: int) -> None:
        if rtl != model:
            raise Mismatch(f"clock {self.chip.cycle}: {what}: rtl={rtl:#x} model={model:#x}")


def run(seed: int, cycles: int) -> None:
    rng = random.Random(seed)
    top = SpiTop()
    host = Host(top)
    for n in range(2):
        random_load(top.chip, host, n, rng)
    host.start(rng.choice([1, 2, 3]))
    while top.cycle < cycles:
        k = rng.random()
        n = rng.randrange(2)
        if k < .3:
            host.push(n, [rng.randrange(1 << 16) for _ in range(rng.randrange(1, 6))])
        elif k < .55:
            count = rng.randrange(1, 6)
            top.spi(header(OP_RX_POP, n, count), [0] * (count + rng.randrange(3)))
        elif k < .65:
            host.read(n, rng.randrange(0x2A), rng.randrange(1, 4))
        elif k < .7:
            host.read(n, 0x26)
        else:
            top.step(rng.randrange(1, 100))
        if rng.random() < .3:
            top.pin_in ^= 1 << rng.choice([0, 1, 2, 3, 4, 5, 6, 7, 11, 12, 13, 14, 15])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--runs", type=int, default=20)
    ap.add_argument("--cycles", type=int, default=20000)
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
