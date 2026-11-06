# Seed global placement: each SM's cells start in the middle of its own column of the core.
#
# On 6x4 the global placement has two outcomes, decided by its initial (quadratic) placement:
# each SM's logic beside its own SRAMs, or the two SMs crossed (SM0 drawn right, SM1 across the
# top), which does not route. Which one a netlist lands on is chance. This script, run by
# Project.SeedPlacement (librelane_plugin_openpio_place.py) before each global placement, puts
# every cell on a net of SM k (a net name with g_sm[k]) and of no other SM at the centre of
# column k of N equal columns (SM k's SRAMs are in column k, src/config.json MACROS), and every
# other movable cell at the centre of the core; PL_SKIP_INITIAL_PLACEMENT then starts the
# Nesterov placement from there instead of from the quadratic one.
import re

import click
from reader import click_odb


@click.command()
@click.option("--pattern", default=r"g_sm\\?\[(\d+)\\?\]", help="Regex on net names; group 1 is the SM's index")
@click_odb
def seed(reader, pattern):
    block = reader.block
    sm = re.compile(pattern)
    owner = {}
    for net in block.getNets():
        m = sm.search(net.getName())
        if not m or net.getSigType() in ("POWER", "GROUND", "CLOCK"):
            continue
        for it in net.getITerms():
            inst = it.getInst()
            if not inst.isFixed() and not inst.getMaster().isBlock():
                owner.setdefault(inst.getName(), set()).add(int(m[1]))
    n = 1 + max((k for ks in owner.values() for k in ks), default=0)
    core = block.getCoreArea()
    w = (core.xMax() - core.xMin()) // n
    yc = (core.yMin() + core.yMax()) // 2
    xc = (core.xMin() + core.xMax()) // 2
    count = [0] * n
    for inst in block.getInsts():
        if inst.isFixed() or inst.getMaster().isBlock():
            continue
        ks = owner.get(inst.getName(), ())
        if len(ks) == 1:
            (k,) = ks
            count[k] += 1
            inst.setLocation(core.xMin() + k * w + w // 2, yc)
        else:
            inst.setLocation(xc, yc)
        inst.setPlacementStatus("PLACED")
    print(f"[INFO] Seeded {' / '.join(map(str, count))} cells in {n} columns, the rest at the centre.")


if __name__ == "__main__":
    seed()
