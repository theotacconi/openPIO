#!/usr/bin/env bash
# Open-source Tang Nano 20K build: Yosys -> nextpnr-himbaechel (Gowin) -> Apicula.
#
#   ./build.sh          synthesize, place & route at 27 MHz, pack build/tang_top.fs
#   ./build.sh load     ... then load into SRAM (lost on power cycle)
#   ./build.sh flash    ... then write to the on-board SPI flash
#
# Tools from oss-cad-suite, or override, e.g. with the pip-installable YoWASP builds:
#   YOSYS=yowasp-yosys NEXTPNR=yowasp-nextpnr-himbaechel-gowin ./build.sh
set -euo pipefail
cd "$(dirname "$0")"

YOSYS=${YOSYS:-yosys}
NEXTPNR=${NEXTPNR:-nextpnr-himbaechel}
GOWIN_PACK=${GOWIN_PACK:-gowin_pack}
LOADER=${LOADER:-openFPGALoader}

DEVICE=GW2AR-LV18QN88C8/I7
FAMILY=GW2A-18C
SRC="../../src/alu.v ../../src/shifter.v ../../src/imem.v ../../src/pau.v \
     ../../src/openpio_core.v ../../src/fifo.v ../../src/spi_cfg_interface.v \
     ../../src/tt_um_openpio.v tang_top.v"

mkdir -p build
$YOSYS -q -l build/yosys.log -p "read_verilog $SRC; synth_gowin -top tang_top -json build/tang_top.json"
$NEXTPNR --json build/tang_top.json --write build/pnr.json --device "$DEVICE" \
    --vopt family=$FAMILY --vopt cst=tang_nano_20k.cst --freq 27 --log build/pnr.log
$GOWIN_PACK -d $FAMILY -o build/tang_top.fs build/pnr.json
grep -E "Max frequency" build/pnr.log | tail -1

case "${1:-}" in
    load)  $LOADER -b tangnano20k build/tang_top.fs ;;
    flash) $LOADER -b tangnano20k -f build/tang_top.fs ;;
    "")    ;;
    *)     echo "usage: $0 [load|flash]" >&2; exit 2 ;;
esac
