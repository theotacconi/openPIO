#!/usr/bin/env bash
# Cell area of a design with the IHP SG13CMOS5L standard cells, as AGENT.md asks for any addition,
# and ABC's estimate of its longest register-to-register logic delay in the slow corner (no wires,
# no SRAM: add the IMEM's 5.3 ns clock-to-output to paths that start at the instruction). Yosys
# runs in the dev container; the Liberty files are fetched once into .pdk_lib/ (git-ignored).
#
#   scripts/area.sh <top> <file.v>...          e.g. scripts/area.sh openpio_sm src/openpio_sm.v
set -euo pipefail
root=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
[ $# -ge 2 ] || { sed -n 2,7p "$0" >&2; exit 1; }
top=$1; shift

typ=.pdk_lib/sg13cmos5l_stdcell_typ_1p20V_25C.lib
slow=.pdk_lib/sg13cmos5l_stdcell_slow_1p08V_125C.lib
for lib in $typ $slow; do
    if [ ! -f "$root/$lib" ]; then
        mkdir -p "$root/.pdk_lib"
        curl -sfL -o "$root/$lib" "https://raw.githubusercontent.com/IHP-GmbH/IHP-Open-PDK/dev/ihp-sg13cmos5l/libs.ref/sg13cmos5l_stdcell/lib/$(basename $lib)"
    fi
done

cd "$root"
"$root/scripts/dev.sh" yosys -q -p "read_verilog -noautowire $*; synth -flatten -top $top; \
    dfflibmap -liberty $typ; abc -liberty $typ; opt_clean; tee -o /dev/stdout stat -liberty $typ" \
    | grep -E "cells|Chip area|sg13cmos5l_[a-z0-9]+_[0-9]" | sed 's/^ *//'
abc="strash;ifraig;scorr;dc2;strash;dch,-f;map,-D,20000;topo;buffer,-p;upsize,-D,20000;dnsize,-D,20000;stime,-p"
"$root/scripts/dev.sh" yosys -p "read_verilog -noautowire $*; synth -flatten -top $top; \
    dfflibmap -liberty $slow; abc -liberty $slow -script +$abc" \
    | sed -n 's/^ABC: .*Delay = *\([0-9.]*\) ps.*/slow-corner logic delay: \1 ps/p'
