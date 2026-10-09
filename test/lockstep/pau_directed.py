"""Directed PAU lockstep scenarios: full USB-LS packets incl. bit-stuffing,
16-bit NRZI OUTs in both shift directions, Manchester back-pressure (PAU_TX)
and deferred IRQs (PAU_BUSY -> IRQ wait) across PAU_BAUD values.

    python3 test/lockstep/pau_directed.py
"""
import collections
import random
import sys

from netlist import ROOT, Lockstep, Netlist, build

from asm import assemble
from emu import StateMachine

net = Netlist(build())
emu_step = StateMachine.step
rng = random.Random(5)
cov: collections.Counter = collections.Counter()
fails = 0


def run(name: str, src: str, cycles: int, words: list, shr: bool = True, **kw) -> None:
    global fails
    sm = StateMachine(assemble(src), **kw)
    sm.shift_out_right = shr
    sm.tx_fifo.extend(words[:1])
    ls = Lockstep(net, sm)
    for cyc in range(cycles):
        reason = sm.stall_reason
        if rng.random() < .1:
            sm.irq_flags[0] = False
        if len(sm.tx_fifo) < 2:
            sm.tx_fifo.append(rng.choice(words))
        if sm.rx_fifo:
            sm.rx_fifo.popleft()
        errs = ls.step(emu_step)
        if sm.stall_reason and sm.stall_reason != reason:
            cov[f"stall {sm.stall_reason}"] += 1
        if reason == "PAU_BUSY" and sm.stall_reason == "IRQ":
            cov["PAU_BUSY -> IRQ wait"] += 1
        if errs:
            fails += 1
            print(f"FAIL {name} cycle {cyc}: {errs}")
            print(sm.get_trace_table(8))
            return
    print(f"ok   {name}: {cycles} cycles")


usb = (ROOT / "agents" / "usb_ls" / "usb_ls.asm").read_text()
for payload in (0xFF80, 0xFE80, 0x7F80, 0xAA80, 0x0080):
    run(f"usb_ls payload {payload:#06x}", usb, 400, [payload], set_base=0, out_base=0)

nrzi16 = """
.wrap_target
    pull block
    set pau_cfg, 3
    set pins, 1
    out pins, 16 [1]
    out pins, 3
    pull block
    set pau_cfg, 1
    out pins, 16
    set pau_cfg, 3
    out pins, 16 [3]
.wrap
"""
for shr in (True, False):
    for base in (0, 3, 7):
        run(f"nrzi 16-bit shr={shr} out_base={base}", nrzi16, 300,
            [0xFFFF, 0xFC3F, 0x7FFE, 0x0000, 0xFDFF], shr=shr, set_base=base,
            out_base=base, open_drain_mask=0x81 if base == 7 else 0)

manchester = """
.side_set 1 opt
    set pau_baud, {baud}
    set pau_cfg, 8
.wrap_target
    pull block
    out pins, {n} side 1
    out pins, {n} [2] side 0
    irq wait 0
    irq 1
    set x, 3
lp: out pins, 2
    jmp x--, lp
    irq clear 1
.wrap
"""
for baud in (0, 1, 3, 7):
    for n in (1, 4, 16):
        for shr in (True, False):
            run(f"manchester baud={baud} n={n} shr={shr}", manchester.format(baud=baud, n=n),
                600, [0xA5C3, 0xFFFF, 0x0001], shr=shr, out_base=2, sideset_base=5,
                clkdiv_int=rng.choice([1, 2]))

print("coverage:", dict(sorted(cov.items())))
print(f"{fails} failing scenarios")
sys.exit(1 if fails else 0)
