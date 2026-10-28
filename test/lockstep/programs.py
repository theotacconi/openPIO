"""The protocol programs in lockstep: every test of programs/*/test_*.py, with the RTL chip core
(src/openpio.v through Verilator) clocked beside the model in each Bench (chip_lockstep.py). A
test passes when its reference devices accept the program and the RTL never differs from the
model on the pads, HOST_IRQ, the host's reads and every register.

    python3 test/lockstep/programs.py [protocol ...]       e.g. uart spi
"""
from __future__ import annotations

import importlib.util
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "test" / "shared"), str(ROOT / "sim" / "model")]
import bench                                                    # noqa: E402
from chip_lockstep import LockstepChip                          # noqa: E402

bench.Bench.chip_class = LockstepChip


def main() -> None:
    wanted = set(sys.argv[1:])
    fails = 0
    for path in sorted(ROOT.glob("programs/*/test_*.py")):
        if wanted and path.parent.name not in wanted:
            continue
        t0 = time.time()
        spec = importlib.util.spec_from_file_location(f"lockstep_{path.stem}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        suite = unittest.defaultTestLoader.loadTestsFromModule(mod)
        result = unittest.TextTestRunner(verbosity=0, stream=open("/dev/null", "w")).run(suite)
        bad = result.failures + result.errors
        fails += len(bad)
        print(f"{path.parent.name}/{path.name}: {result.testsRun - len(bad)}/{result.testsRun} "
              f"in lockstep, {time.time() - t0:.0f} s")
        for test, trace in bad:
            print(f"  {test.id()}: {trace.strip().splitlines()[-1]}")
    raise SystemExit(1 if fails else 0)


if __name__ == "__main__":
    main()
