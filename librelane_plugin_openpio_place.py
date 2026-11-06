"""
LibreLane plugin: start each global placement with every SM's cells in its own column of the core.

Project.SeedPlacement runs odb_seed.py (see there why). src/config.json inserts it before both
global placements (the pattern matches OpenROAD.GlobalPlacementSkipIO and OpenROAD.GlobalPlacement)
and sets PL_SKIP_INITIAL_PLACEMENT, so the placement starts from the seed:

    "meta": { "substituting_steps": { "-OpenROAD.GlobalPlacement*": "Project.SeedPlacement" } }

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
