# cocotb testbench (Tiny Tapeout template layout, see test.py)
#
#   make -B                RTL simulation of ../src
#   make -B GATES=yes      gate-level simulation of gate_level_netlist.v (the hardened
#                          design, from the tt_submission artifact of a gds run) with the
#                          IHP cell models under $(PDK_ROOT); in CI the gl_test job does this
#
# Results: results.xml (pass/fail per test), tb.fst (waveforms).
# Docs: https://docs.cocotb.org/en/stable/quickstart.html

SIM ?= icarus
FST ?= -fst
TOPLEVEL_LANG ?= verilog
SRC_DIR = $(PWD)/../src
# Same list as source_files in info.yaml
PROJECT_SOURCES = tt_um_openpio.v spi_cfg_interface.v fifo.v openpio_core.v pau.v shifter.v alu.v imem.v

ifneq ($(GATES),yes)

# RTL simulation:
SIM_BUILD				= sim_build/rtl
VERILOG_SOURCES += $(addprefix $(SRC_DIR)/,$(PROJECT_SOURCES))

else

# Gate level simulation:
SIM_BUILD				= sim_build/gl
COMPILE_ARGS    += -DGL_TEST
COMPILE_ARGS    += -DFUNCTIONAL
COMPILE_ARGS    += -DSIM
VERILOG_SOURCES += $(PDK_ROOT)/ihp-sg13cmos5l/libs.ref/sg13cmos5l_io/verilog/sg13cmos5l_io.v
VERILOG_SOURCES += $(PDK_ROOT)/ihp-sg13cmos5l/libs.ref/sg13cmos5l_stdcell/verilog/sg13cmos5l_udp.v
VERILOG_SOURCES += $(PDK_ROOT)/ihp-sg13cmos5l/libs.ref/sg13cmos5l_stdcell/verilog/sg13cmos5l_stdcell.v

# this gets copied in by the GDS action workflow
VERILOG_SOURCES += $(PWD)/gate_level_netlist.v

endif

COMPILE_ARGS 		+= -I$(SRC_DIR)

VERILOG_SOURCES += $(PWD)/tb.v
COCOTB_TOPLEVEL = tb
COCOTB_TEST_MODULES = test

include $(shell cocotb-config --makefiles)/Makefile.sim
