# A buffer at every SRAM data output: each A_DOUT pin drives one buffer in the first row past
# the macro's pin edge, and the buffer drives what the pin drove.
#
# A_DOUT's max capacitance is 0.064 pF. The design repair sees that limit only through placement
# and global-route estimates, about half the capacitance a net has once detailed routing has
# drawn it: on 8x4 four A_DOUT nets ended at 0.064-0.084 pF, and a smaller repair margin or a
# shorter maximum wire only moved the violations. A buffer at the pin leaves A_DOUT a few
# microns of wire and one input. Run by Project.BufferSramOutputs
# (librelane_plugin_openpio_place.py) after global placement; the new nets and instances are
# named sram_dout_*, and RSZ_DONT_TOUCH_RX keeps the resizer from removing or resizing them.
# Detailed placement then legalizes the buffers next to the pins.
import re

import click
import odb
from reader import click_odb


@click.command()
@click.option("--macro", "macro_rx", default=r"^RM_IHPSG13_", help="Regex on the macro master names")
@click.option("--pin", "pin_rx", default=r"^A_DOUT\[\d+\]$", help="Regex on their output pin names")
@click.option("--cell", default="sg13cmos5l_buf_4", help="Buffer master")
@click_odb
def buffer(reader, macro_rx, pin_rx, cell):
    block, db = reader.block, reader.db
    master = db.findMaster(cell)
    if master is None:
        raise click.ClickException(f"no master {cell}")
    a = next(m for m in master.getMTerms() if m.getIoType() == "INPUT" and m.getSigType() == "SIGNAL")
    x_term = next(m for m in master.getMTerms() if m.getIoType() == "OUTPUT")
    rows = list(block.getRows())
    count = 0
    for inst in block.getInsts():
        if not inst.getMaster().isBlock() or not re.search(macro_rx, inst.getMaster().getName()):
            continue
        box = inst.getBBox()
        for it in inst.getITerms():
            net = it.getNet()
            if net is None or not re.search(pin_rx, it.getMTerm().getName()):
                continue
            pin = it.getBBox()
            above = (pin.yMin() + pin.yMax()) // 2 > (box.yMin() + box.yMax()) // 2
            # the nearest row on the pin's side of the macro, under the pin's x
            cand = [r for r in rows
                    if (r.getBBox().yMin() >= box.yMax() if above else r.getBBox().yMax() <= box.yMin())
                    and r.getBBox().xMin() <= pin.xMin() <= r.getBBox().xMax() - master.getWidth()]
            if not cand:
                raise click.ClickException(f"no row by {inst.getName()}/{it.getMTerm().getName()}")
            row = min(cand, key=lambda r: abs(r.getBBox().yMin() - (box.yMax() if above else box.yMin())))
            site = row.getSpacing()
            x0 = row.getBBox().xMin()
            x = x0 + (pin.xMin() - x0) // site * site
            name = "sram_dout_" + re.sub(r"\W+", "_", f"{inst.getName()}_{it.getMTerm().getName()}").strip("_")
            buf = odb.dbInst.create(block, master, name)
            new = odb.dbNet.create(block, name)
            new.setSigType("SIGNAL")
            it.disconnect()
            it.connect(new)
            buf.findITerm(a.getName()).connect(new)
            buf.findITerm(x_term.getName()).connect(net)
            buf.setOrient(row.getOrient())
            buf.setLocation(x, row.getBBox().yMin())
            buf.setPlacementStatus("PLACED")
            count += 1
    print(f"[INFO] Buffered {count} SRAM outputs with {cell}.")


if __name__ == "__main__":
    buffer()
