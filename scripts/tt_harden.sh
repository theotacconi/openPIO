#!/usr/bin/env bash
# Local Tiny Tapeout IHP SG13G2 hardening, mirroring TinyTapeout/tt-gds-action@ttihp26b:
#   1. tt-support-tools: validate info.yaml/docs, write src/config_merged.json
#   2. LibreLane in its container (docker or rootless podman)
#   3. sign-off summary: DRC, LVS, antenna, setup/hold slack per corner
#
#   scripts/tt_harden.sh                (run from anywhere inside the repo)
#
# Env overrides:
#   TT_TOOLS_REF       tt-support-tools branch/tag     (main, as the action)
#   LIBRELANE_VERSION  LibreLane container tag         (3.0.5, as the action)
#   PDK_ROOT           PDK cache, fetched by ciel      ($HOME/.ciel)
#   ENGINE             container engine                (docker, else podman)
#   PYTHON             host Python for tt-support-tools (python3; 3.11/3.12 match its pins)
#
# Outputs: runs/wokwi/ (LibreLane run), runs/wokwi/final/gds/<top>.gds
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

TT_TOOLS_REF=${TT_TOOLS_REF:-main}
LL_VERSION=${LIBRELANE_VERSION:-3.0.5}
PDK_ROOT=${PDK_ROOT:-$HOME/.ciel}
ENGINE=${ENGINE:-$(command -v docker || command -v podman || true)}
PYTHON=${PYTHON:-python3}
[ -n "$ENGINE" ] || { echo "need docker or podman" >&2; exit 1; }

# ---- 1. tt-support-tools and merged LibreLane config ------------------------
if [ ! -d tt/.git ]; then
    git clone -q https://github.com/TinyTapeout/tt-support-tools tt
fi
git -C tt fetch -q origin "$TT_TOOLS_REF"
git -C tt checkout -q FETCH_HEAD

if [ ! -x tt/.venv/bin/python ]; then
    "$PYTHON" -m venv tt/.venv
    if ! tt/.venv/bin/pip install -q -r tt/requirements.txt; then
        # Pinned wheels missing for this Python (e.g. numpy 1.26 on >= 3.13): same set, unpinned.
        echo "tt_harden: pinned requirements failed, installing unpinned" >&2
        grep -vE '^\s*(#|$)' tt/requirements.txt | sed -E 's/[=<>].*//' | xargs tt/.venv/bin/pip install -q
    fi
fi
export PATH="$PWD/tt/.venv/bin:$PATH"   # tt_tool runs yowasp-yosys from here

python tt/tt_tool.py --ihp --check-docs
python tt/tt_tool.py --ihp --create-user-config

# ---- 2. LibreLane flow (what tt_tool --harden --ihp runs) -------------------
mkdir -p "$PDK_ROOT"
rm -rf runs/wokwi && mkdir -p runs/wokwi
engine_args=(run --rm -v "$PWD:$PWD:Z" -v "$PDK_ROOT:$PDK_ROOT:Z" -w "$PWD" -e PDK_ROOT="$PDK_ROOT")
[[ $(basename "$ENGINE") == podman ]] && engine_args+=(--userns=keep-id)
"$ENGINE" "${engine_args[@]}" "ghcr.io/librelane/librelane:$LL_VERSION" \
    python3 -m librelane --pdk-root "$PDK_ROOT" --pdk ihp-sg13g2 \
    --run-tag wokwi --force-run-dir runs/wokwi --hide-progress-bar src/config_merged.json

# tt_tool --harden post-processing: pdk.json, commit_id.json
python - <<'EOF_PY'
import json, subprocess, sys
sys.path.insert(0, "tt")
from tech import tech_map
run = "runs/wokwi"
ll = json.load(open(f"{run}/resolved.json"))
v = tech_map["ihp-sg13g2"].read_pdk_version(ll["PDK_ROOT"])
json.dump({"FLOW_NAME": "LibreLane", "FLOW_VERSION": ll["meta"]["librelane_version"],
           "PDK": ll["PDK"], "PDK_SOURCE": v["source"], "PDK_VERSION": v["version"]},
          open(f"{run}/pdk.json", "w"), indent=2)
commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
json.dump({"app": "Tiny Tapeout (scripts/tt_harden.sh)", "repo": "", "commit": commit,
           "workflow_url": ""}, open(f"{run}/final/commit_id.json", "w"), indent=2)
EOF_PY
python tt/tt_tool.py --ihp --create-tt-submission    # -> tt_submission/

# ---- 3. sign-off summary -----------------------------------------------------
python3 scripts/tt_signoff.py runs/wokwi
