"""Run every agents/*/test_*.py unittest with each emu step mirrored on the
gate-level core; any per-cycle state divergence fails the run.

    python3 test/lockstep/agents_replay.py
"""
import glob
import importlib.util
import sys
import unittest

from netlist import ROOT, Lockstep, Netlist, build

import emu

net = Netlist(build())
emu_step = emu.StateMachine.step
results: dict = {}


def _test_id() -> str:
    f = sys._getframe()
    while f:
        s = f.f_locals.get("self")
        if isinstance(s, unittest.TestCase):
            return s.id().split(".", 1)[-1]
        f = f.f_back
    return "?"


def lockstep_step(sm) -> None:
    r = results.setdefault(_test_id(), {"cycles": 0, "err": None})
    if not hasattr(sm, "_lockstep"):
        sm._lockstep = Lockstep(net, sm)
    errs = sm._lockstep.step(emu_step)
    r["cycles"] += 1
    if errs and r["err"] is None:
        r["err"] = f"cycle {sm._lockstep.cycle}: {errs}"


emu.StateMachine.step = lockstep_step

suite = unittest.TestSuite()
for path in sorted(glob.glob(str(ROOT / "agents" / "*" / "test_*.py"))):
    spec = importlib.util.spec_from_file_location(path.split("/")[-1][:-3], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(mod))

with open("/dev/null", "w") as sink:
    run = unittest.TextTestRunner(stream=sink, verbosity=0).run(suite)

bad = 0
for name, r in sorted(results.items()):
    status = "ok" if r["err"] is None else f"MISMATCH {r['err']}"
    bad += r["err"] is not None
    print(f"{name:58s} {r['cycles']:5d} cycles  {status}")
for case, tb in run.failures + run.errors:
    print(f"FAIL {case.id()}: {tb.strip().splitlines()[-1]}")
print(f"{run.testsRun} tests, {len(run.failures) + len(run.errors)} failed, {bad} lockstep mismatches")
sys.exit(1 if bad or run.failures or run.errors else 0)
