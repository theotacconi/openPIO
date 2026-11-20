# Decap cells placed before routing, in part of every gap between standard cells, so the router
# sees them and draws its Metal1 around them.
#
# A decap's VDD and VSS are Metal1 shapes over most of the cell. LibreLane's fill insertion
# (DECAP_CELLS) runs after detailed routing and drops decaps into gaps a Metal1 route may cross:
# on 6x4 it shorted 3859 nets to the supply. Routing from Metal2 instead keeps Metal1 for the
# cells, but Metal3, the only other horizontal layer under TT's Metal4 cap, then overflows (on
# 8x4: 547 global-route overflows, 10 detailed-route violations left). The routes need little of
# Metal1 (12 % on 8x4) and only in the gaps: a decap in part of each gap leaves them the rest.
#
# Each gap in a row, less --margin sites by each neighbour (the neighbours' pins stay reachable),
# gets decaps over --fraction of it, centred. No decap within --halo of a macro, where the SRAM
# pins and their buffers are. Run by Project.InsertDecaps (librelane_plugin_openpio_place.py)
# after clock tree synthesis and its timing repair, before global routing; detailed placement
# after the post-route repair may move them like any cell. Fill insertion fills what is left.
# Power: every OpenROAD step's read_current_odb applies the global connections (VDD -> VPWR,
# VSS -> VGND) to the new instances; LVS checks them.
from collections import defaultdict

import click
import odb
from reader import click_odb


@click.command()
@click.option("--cells", default="sg13cmos5l_decap_8,sg13cmos5l_decap_4",
              help="Decap masters, widest first")
@click.option("--fraction", type=float, default=0.5, help="Share of each gap to fill")
@click.option("--margin", type=int, default=2, help="Free sites left by each neighbour")
@click.option("--halo", type=float, default=10.0, help="Microns kept free around each macro")
@click.option("--min-gap", type=int, default=0, help="Gaps narrower than this, in sites, stay free")
@click_odb
def decaps(reader, cells, fraction, margin, halo, min_gap):
    block, db = reader.block, reader.db
    masters = []
    for name in cells.split(","):
        m = db.findMaster(name)
        if m is None:
            raise click.ClickException(f"no master {name}")
        masters.append(m)
    masters.sort(key=lambda m: -m.getWidth())
    h = int(halo * block.getDbUnitsPerMicron())

    busy = defaultdict(list)                     # row index -> [(x0, x1)] taken
    rows = list(block.getRows())

    def take(box, grow=0):
        for i, r in enumerate(rows):
            rb = r.getBBox()
            if box.yMin() - grow < rb.yMax() and rb.yMin() < box.yMax() + grow:
                busy[i].append((box.xMin() - grow, box.xMax() + grow))

    for inst in block.getInsts():
        if not inst.isPlaced():
            continue
        take(inst.getBBox(), h if inst.getMaster().isBlock() else 0)
    for b in block.getBlockages():
        take(b.getBBox())

    count, area = 0, 0
    for i, r in enumerate(rows):
        rb, site = r.getBBox(), r.getSpacing()
        x0 = rb.xMin()
        spans = sorted(busy[i]) + [(rb.xMax(), rb.xMax())]
        cur = x0
        for a, b in spans:
            if a - cur >= max(min_gap, 1) * site:
                lo = x0 + -(-(cur - x0) // site) * site + margin * site   # first free site
                hi = x0 + (a - x0) // site * site - margin * site         # past the last
                want = int((hi - lo) * fraction) // site * site
                x = lo + ((hi - lo) - want) // 2 // site * site
                end = x + want
                for m in masters:
                    while x + m.getWidth() <= end:
                        inst = odb.dbInst.create(block, m, f"decap_pre_{count}")
                        inst.setOrient(r.getOrient())
                        inst.setLocation(x, rb.yMin())
                        inst.setPlacementStatus("PLACED")
                        x += m.getWidth()
                        count += 1
                        area += m.getWidth() * m.getHeight()
            cur = max(cur, b)
    dbu = block.getDbUnitsPerMicron()
    print(f"[INFO] Placed {count} decap cells, {area / dbu / dbu:.0f} um^2.")


if __name__ == "__main__":
    decaps()
