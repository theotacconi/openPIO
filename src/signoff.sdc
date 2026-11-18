# Sign-off constraints: the clock the chip is promised in every corner.
#
# Placement, clock tree and routing run at CLOCK_PERIOD (src/config.json),
# tighter than this: OpenROAD repairs timing for the typical corner only, and
# a path is about 1.57 times slower in the slow corner (1.08 V, 125 C), so a
# looser target leaves the slow corner as far behind as the clock it gives
# up. Built for 60 MHz, the slow corner allowed 48 MHz; built for 50, 42.
# The sign-off steps (OpenROAD.STAPostPNR) read this file instead: the
# generic constraints of LibreLane at this period.
set signoff_period 22.0
set ::env(CLOCK_PERIOD) $signoff_period
source $::env(SCRIPTS_DIR)/base.sdc
