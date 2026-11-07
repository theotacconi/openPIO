# A buffer at every SRAM signal pin, in the first row past the macro's pin edge: an A_DOUT drives
# one buffer, which drives what the pin drove; an input pin (A_ADDR, A_DIN, A_WEN, A_REN) is
# driven by one buffer, which takes what drove the pin.
#
# The design repair sees the SRAM's pin limits only through placement and global-route
# estimates, about half the capacitance a net has once detailed routing has drawn it. A_DOUT's
# max capacitance is 0.064 pF: on 8x4 four A_DOUT nets ended at 0.064-0.084 pF, and a smaller
# repair margin or a shorter maximum wire only moved the violations. An input pin's max slew is
# 0.476 ns (typical), 0.595 ns (slow): long nets from small gates went over it on 1-4 pins a run
# on 6x4, and on 8x4 the IMEM data inputs, one host bus to both IMEMs across the die, reached
# 88 % of it in the slow corner. A buffer at the pin leaves the SRAM pin a few microns of wire,
# and the long net ends at a standard-cell input (max slew 2.5 ns), which the repair handles.
# Run by Project.BufferSramPins (librelane_plugin_openpio_place.py) after global placement. The
# short nets between buffer and pin are named sram_pin_*, and RSZ_DONT_TOUCH_RX keeps the resizer
# off them; the buffers, sram_buf_*, stay free: the resizer may size them and buffer the long net
# they end (it stops with an error on a net whose load is a dont_touch instance). Detailed
# placement then legalizes the buffers.
import re

import click
import odb
from reader import click_odb


@click.command()
@click.option("--macro", "macro_rx", default=r"^RM_IHPSG13_", help="Regex on the macro master names")
@click.option("--pin", "pin_rx", default=r"^A_(DOUT|DIN|ADDR)\[\d+\]$|^A_[WR]EN$",
              help="Regex on their signal pin names")
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
            name = re.sub(r"\W+", "_", f"{inst.getName()}_{it.getMTerm().getName()}").strip("_")
            buf = odb.dbInst.create(block, master, "sram_buf_" + name)       # Verilog: an instance
            new = odb.dbNet.create(block, "sram_pin_" + name)                # and a net, two names
            new.setSigType("SIGNAL")
            it.disconnect()
            it.connect(new)
            out = it.getIoType() == "OUTPUT"         # A_DOUT -> buffer -> its loads
            buf.findITerm(a.getName()).connect(new if out else net)
            buf.findITerm(x_term.getName()).connect(net if out else new)
            buf.setOrient(row.getOrient())
            buf.setLocation(x, row.getBBox().yMin())
            buf.setPlacementStatus("PLACED")
            count += 1
    print(f"[INFO] Buffered {count} SRAM pins with {cell}.")


if __name__ == "__main__":
    buffer()
