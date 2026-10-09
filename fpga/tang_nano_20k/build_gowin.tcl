# Gowin EDA build (alternative to build.sh):  gw_sh build_gowin.tcl
# Output: impl/pnr/openpio_tang.fs
set_device -name GW2AR-18C GW2AR-LV18QN88C8/I7

foreach f {alu shifter imem pau openpio_core fifo spi_cfg_interface tt_um_openpio} {
    add_file ../../src/$f.v
}
add_file tang_top.v
add_file tang_nano_20k.cst
add_file tang_nano_20k.sdc

set_option -top_module tang_top
set_option -verilog_std v2001
set_option -output_base_name openpio_tang

run all
