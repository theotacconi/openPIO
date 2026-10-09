"""Random-program lockstep fuzz: random instruction words, configs and host
activity (pins, FIFOs, IRQ clears), compared against emu.py every clock.

    python3 test/lockstep/fuzz.py [--seed N] [--programs N] [--cycles N] [--xval 0|1]
"""
import argparse
import random
import sys
import time

from netlist import Lockstep, Netlist, build

from emu import StateMachine

ap = argparse.ArgumentParser()
ap.add_argument("--seed", type=int, default=1)
ap.add_argument("--programs", type=int, default=100)
ap.add_argument("--cycles", type=int, default=400)
ap.add_argument("--xval", type=int, default=0, help="value for netlist don't-care constants")
args = ap.parse_args()

net = Netlist(build(), xval=args.xval)
emu_step = StateMachine.step
rng = random.Random(args.seed)


def rand_word(baud: int) -> int:
    op = rng.randrange(8)
    w = (op << 13) | rng.randrange(1 << 13)
    dest = (w >> 5) & 7
    if op == 7 and dest == 5:                    # SET PAU_BAUD: one value per program
        w = (w & ~0x1F) | baud
    if op == 7 and dest == 3 and rng.random() < .8:
        w = (w & ~0x1F) | rng.randrange(4)       # mostly small CLKDIV
    if rng.random() < .5:
        w &= ~(0x1F << 8)                        # zero delay / side-set
    return w


fails = cut = total = 0
t0 = time.time()
for t in range(args.programs):
    ss_count = rng.choice([0, 0, 1, 2, 3])
    baud = rng.choice([0, 1, 2, 3])
    n = rng.randrange(4, 33)
    prog = [rand_word(baud) for _ in range(n)]
    sm = StateMachine(
        prog, side_set_count=ss_count, side_set_opt=ss_count > 0 and rng.random() < .4,
        side_set_pindirs=rng.random() < .2,
        open_drain_mask=rng.choice([0, 0, rng.randrange(256)]),
        clkdiv_int=rng.choice([1, 1, 1, 2, 3]),
        clkdiv_frac=rng.choice([0, 0, 0, rng.randrange(256)]),
        in_base=rng.randrange(8), out_base=rng.randrange(8), set_base=rng.randrange(8),
        set_count=rng.randrange(6), sideset_base=rng.randrange(8), jmp_pin=rng.randrange(8))
    sm.wrap_bottom = rng.randrange(n)
    sm.wrap_top = rng.randrange(sm.wrap_bottom, n)
    sm.shift_in_right, sm.shift_out_right = rng.random() < .5, rng.random() < .5
    ls = Lockstep(net, sm)
    for cyc in range(args.cycles):
        if rng.random() < .2: sm.gpio_in ^= 1 << rng.randrange(8)
        if rng.random() < .15 and len(sm.tx_fifo) < 4: sm.tx_fifo.append(rng.randrange(1 << 16))
        if rng.random() < .15 and sm.rx_fifo: sm.rx_fifo.popleft()
        if rng.random() < .05: sm.irq_flags[rng.randrange(8)] = False
        if rng.random() < .05: sm.peer_irq_flags[rng.randrange(8)] = False
        queued, hp = len(sm.pau_tx_queue), sm.pau_baud or 1
        errs = ls.step(emu_step)
        total += 1
        if queued and (sm.pau_baud or 1) != hp:
            cut += 1        # PAU_BAUD changed with samples queued: unsupported (pau.v)
            break
        if errs:
            fails += 1
            print(f"program {t} cycle {cyc}: {errs}")
            print(sm.get_trace_table(6))
            print("prog", [hex(w) for w in prog], "wrap", sm.wrap_bottom, sm.wrap_top)
            break
    if fails >= 3:
        break
print(f"seed {args.seed}: {total} cycles, {t + 1} programs, {fails} failing, "
      f"{cut} stopped at PAU_BAUD change, {time.time() - t0:.0f}s")
sys.exit(1 if fails else 0)
