"""
LibreLane plugin: placement steps of openPIO.

Project.SeedPlacement runs odb_seed.py: each global placement starts with every SM's cells in
its own column of the core. src/config.json inserts it before both global placements (the
pattern matches OpenROAD.GlobalPlacementSkipIO and OpenROAD.GlobalPlacement) and sets
PL_SKIP_INITIAL_PLACEMENT, so the placement starts from the seed.

Project.BufferSramOutputs runs odb_sram_buf.py after the global placement: a buffer at every
SRAM data output, so A_DOUT keeps under its max capacitance. RSZ_DONT_TOUCH_RX (^sram_dout_)
keeps the resizer off them.

    "meta": { "substituting_steps": {
        "-OpenROAD.GlobalPlacement*": "Project.SeedPlacement",
        "+OpenROAD.GlobalPlacement": "Project.BufferSramOutputs" } }

LibreLane imports every module on the Python path whose name starts with ``librelane_plugin_``
(librelane_plugin_openpio_pdn.py).
"""
import os

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
class BufferSramOutputs(OdbpyStep):
    id = "Project.BufferSramOutputs"
    name = "Buffer the SRAM Data Outputs"

    def get_script_path(self):
        return os.path.join(HERE, "odb_sram_buf.py")
