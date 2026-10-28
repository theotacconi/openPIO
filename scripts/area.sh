#!/usr/bin/env bash
# Cell area of a design with the IHP SG13CMOS5L standard cells (typical corner), as AGENT.md asks
# for any addition. Yosys runs in the dev container; the Liberty file is fetched once into
# .pdk_lib/ (git-ignored). No timing: the area of the mapped cells, by cell type.
#
#   scripts/area.sh <top> <file.v>...          e.g. scripts/area.sh openpio_sm src/openpio_sm.v
set -euo pipefail
root=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
[ $# -ge 2 ] || { sed -n 2,6p "$0" >&2; exit 1; }
top=$1; shift

lib=.pdk_lib/sg13cmos5l_stdcell_typ_1p20V_25C.lib
if [ ! -f "$root/$lib" ]; then
    mkdir -p "$root/.pdk_lib"
    curl -sfL -o "$root/$lib" "https://raw.githubusercontent.com/IHP-GmbH/IHP-Open-PDK/dev/ihp-sg13cmos5l/libs.ref/sg13cmos5l_stdcell/lib/$(basename $lib)"
fi

cd "$root"
"$root/scripts/dev.sh" yosys -q -p "read_verilog -noautowire $*; synth -flatten -top $top; \
    dfflibmap -liberty $lib; abc -liberty $lib; opt_clean; tee -o /dev/stdout stat -liberty $lib" \
    | grep -E "cells|Chip area|sg13cmos5l_[a-z]+[0-9]*_[0-9]" | sed 's/^ *//'
