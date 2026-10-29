#!/usr/bin/env python3
"""Sign-off summary of a LibreLane run: DRC, LVS, antenna, timing, area.

    gh run download <run-id> -n GDS_logs -D /tmp/gds_logs    # gds workflow artifact
    python3 scripts/tt_signoff.py /tmp/gds_logs/runs/wokwi

Reads <run>/final/metrics.json. Exit status 1 if any DRC/LVS/antenna error
count is non-zero, the hold worst slack is negative in any corner, or the setup
worst slack or the max slew violation count is negative / non-zero in the
typical or fast corner. The clock is 50 MHz nominal (typical corner, 1.2 V,
25 C); in the slow corner the setup slack is reported with the clock it allows,
and slew violations (nets detailed routing lengthened, with no repair after
it) are reported, not checked.
"""
import json
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

PERIOD = 20.0                                     # ns, src/config.json CLOCK_PERIOD
SLOW = "nom_slow_1p08V_125C"
for key in sorted(k for k in metrics if k.startswith("design__max_slew_violation__count__corner:")):
    val = int(metrics[key])
    if key.endswith(SLOW):
        note = "info" if val else "ok"
    else:
        failed |= val != 0
        note = "FAIL" if val else "ok"
    print(f"  {key:60s} {val:10d}     {note}")
for kind in ("setup", "hold"):
    for key in sorted(k for k in metrics if k.startswith(f"timing__{kind}__ws__corner:")):
        val = float(metrics[key])
        if kind == "setup" and key.endswith(SLOW):
            note = f"info: {1e3 / (PERIOD - val):.0f} MHz max" if val < 0 else "ok"
        else:
            failed |= val < 0
            note = "FAIL" if val < 0 else "ok"
        print(f"  {key:60s} {val:10.3f} ns  {note}")

for key, label in INFO:
    if key in metrics:
        print(f"  {label:28s} {metrics[key]}")

print("SIGN-OFF", "FAILED" if failed else "PASSED")
sys.exit(1 if failed else 0)
