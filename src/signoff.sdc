# Sign-off constraints: the clock the chip is promised in every corner.
#
# Placement, clock tree and routing run at CLOCK_PERIOD (src/config.json),
# tighter than this: OpenROAD repairs timing for the typical corner only, and
# a path is about 1.57 times slower in the slow corner (1.08 V, 125 C), so a
# looser target leaves the slow corner as far behind as the clock it gives
# up. Built for 60 MHz, the slow corner allowed 48 MHz; built for 50, 42.
# The sign-off steps (OpenROAD.STAPostPNR) read this file instead: the
# generic constraints of LibreLane at this period, and one multicycle path.
set signoff_period 20.0
set ::env(CLOCK_PERIOD) $signoff_period
source $::env(SCRIPTS_DIR)/base.sdc

# The host's write data, spi.v's w = {sr, s_mosi[1]}: 16 flip-flops whose
# value nothing reads in the clock after it changes (sr moves on an SCLK rise,
# and no rise or host access follows in the next clock; MOSI moves while SCLK
# is low). Proved on the RTL by test/formal/host_wdata_top.ys: every reader,
# given any value for a bit in the clock after it changed, leaves the chip
# the same. So their paths have two clocks; hold stays at the launch edge.
# Before this, all 80 endpoints short of 2 ns at 22 ns started at one of them
# (slow corner -1.13 ns at 20 ns; with it +0.12 ns, same layout).
# Each h_wdata net must be driven straight by its flip-flop: a buffer or gate
# put in between (another netlist) leaves fewer than 16 here and stops the
# sign-off rather than constrain the wrong cells.
set host_wdata_regs [get_cells -of_objects \
    [get_pins -filter "direction==output && lib_pin_name==Q" -of_objects [get_nets {h_wdata[*]}]]]
if {[llength $host_wdata_regs] != 16} {
    error "signoff.sdc: [llength $host_wdata_regs] flip-flops drive h_wdata, not 16"
}
set_multicycle_path -setup 2 -from $host_wdata_regs
set_multicycle_path -hold 1 -from $host_wdata_regs
