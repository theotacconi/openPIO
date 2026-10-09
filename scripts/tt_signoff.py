#!/usr/bin/env python3
"""Sign-off summary of a LibreLane run: DRC, LVS, antenna, timing, area.

    python3 scripts/tt_signoff.py runs/wokwi

Reads <run>/final/metrics.json. Exit status 1 if any DRC/LVS/antenna error
count is non-zero or any setup/hold worst slack (all corners) is negative.
"""
import json
import re
import sys
from pathlib import Path

run = Path(sys.argv[1] if len(sys.argv) > 1 else "runs/wokwi")
metrics_file = run / "final" / "metrics.json"
if not metrics_file.exists():
    sys.exit(f"{metrics_file} not found: the LibreLane run did not complete")
metrics = json.loads(metrics_file.read_text())

ERROR_COUNTS = [
    ("route__drc_errors", "detailed-route DRC"),
    ("magic__drc_error__count", "Magic DRC"),
    ("klayout__drc_error__count", "KLayout DRC"),
    ("design__lvs_error__count", "LVS errors"),
    ("design__lvs_unmatched_pin__count", "LVS unmatched pins"),
    ("antenna__violating__nets", "antenna violating nets"),
    ("antenna__violating__pins", "antenna violating pins"),
    ("design__max_slew_violation__count", "max slew violations"),
    ("design__max_cap_violation__count", "max cap violations"),
]
INFO = [
    ("design__instance__count__stdcell", "std cells (excl. fill)"),
    ("design__instance__area__stdcell", "std cell area (um^2)"),
    ("design__core__area", "core area (um^2)"),
    ("design__instance__utilization__stdcell", "core utilisation"),
    ("design__instance__count__hold_buffer", "hold buffers"),
    ("power__total", "total power (W)"),
    ("route__wirelength", "wirelength (um)"),
]

failed = False
print(f"== {run}")
for key, label in ERROR_COUNTS:
    if key in metrics:
        val = metrics[key]
        bad = val not in (0, "0", None)
        failed |= bad
        print(f"  {label:28s} {val!s:>12}  {'FAIL' if bad else 'ok'}")
    else:
        print(f"  {label:28s} {'(not run)':>12}")

slack = {k: v for k, v in metrics.items()
         if re.fullmatch(r"timing__(setup|hold)__ws(__corner:.*)?", k)}
for key in sorted(slack):
    val = slack[key]
    bad = val is not None and float(val) < 0
    failed |= bad
    print(f"  {key:60s} {float(val):10.3f} ns  {'FAIL' if bad else 'ok'}")

for key, label in INFO:
    if key in metrics:
        print(f"  {label:28s} {metrics[key]}")

print("SIGN-OFF", "FAILED" if failed else "PASSED")
sys.exit(1 if failed else 0)
