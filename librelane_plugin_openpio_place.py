"""
LibreLane plugin: placement steps of openPIO.

Project.SeedPlacement runs odb_seed.py: each global placement starts with every SM's cells in
its own column of the core. src/config.json inserts it before both global placements (the
pattern matches OpenROAD.GlobalPlacementSkipIO and OpenROAD.GlobalPlacement) and sets
PL_SKIP_INITIAL_PLACEMENT, so the placement starts from the seed.

Project.BufferSramPins runs odb_sram_buf.py after the global placement: a buffer at every SRAM
signal pin, so A_DOUT keeps under its max capacitance and the inputs under their max slew.
RSZ_DONT_TOUCH_RX (^sram_pin_) keeps the resizer off the short nets from
buffer to pin.

Project.InsertDecaps runs odb_decap.py before global routing: decap cells in part of every gap
between standard cells, so the router draws its Metal1 around them.

    "meta": { "substituting_steps": {
        "-OpenROAD.GlobalPlacement*": "Project.SeedPlacement",
        "+OpenROAD.GlobalPlacement": "Project.BufferSramPins",
        "-OpenROAD.GlobalRouting": "Project.InsertDecaps" } }

LibreLane imports every module on the Python path whose name starts with ``librelane_plugin_``
(librelane_plugin_openpio_pdn.py).
"""
import os
from decimal import Decimal

from librelane.config import Variable
from librelane.steps import Step
from librelane.steps.odb import OdbpyStep

HERE = os.path.dirname(os.path.abspath(__file__))


@Step.factory.register()
class SeedPlacement(OdbpyStep):
    id = "Project.SeedPlacement"
    name = "Seed Global Placement Per SM"

    def get_script_path(self):
        return os.path.join(HERE, "odb_seed.py")


@Step.factory.register()
class BufferSramPins(OdbpyStep):
    id = "Project.BufferSramPins"
    name = "Buffer the SRAM Signal Pins"

    def get_script_path(self):
        return os.path.join(HERE, "odb_sram_buf.py")


@Step.factory.register()
class InsertDecaps(OdbpyStep):
    id = "Project.InsertDecaps"
    name = "Insert Decap Cells Before Routing"

    def get_script_path(self):
        return os.path.join(HERE, "odb_decap.py")

    config_vars = [
        Variable("PRE_ROUTE_DECAP_FRACTION", Decimal, "Share of each gap between standard cells given to decap cells.", default=Decimal("0.5")),
        Variable("PRE_ROUTE_DECAP_MIN_GAP", int, "Gaps narrower than this stay free of decaps.", units="sites", default=0),
        Variable("PRE_ROUTE_DECAP_MARGIN", int, "Free sites left beside each standard cell.", units="sites", default=2),
    ]

    def get_command(self):
        return super().get_command() + [
            "--fraction", str(self.config["PRE_ROUTE_DECAP_FRACTION"]),
            "--min-gap", str(self.config["PRE_ROUTE_DECAP_MIN_GAP"]),
            "--margin", str(self.config["PRE_ROUTE_DECAP_MARGIN"]),
        ]
