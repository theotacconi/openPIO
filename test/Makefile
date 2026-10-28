# cocotb testbench (Tiny Tapeout template layout, see test.py)
#
#   make -B                RTL simulation of ../src, memories = imem.v, fifo_ram.v (flip-flop models)
#   make -B IMEM=sram      same with the chip's memories: *_sram.v + IHP's SRAM models
#                          under $(PDK_ROOT)
#   make -B GATES=yes      gate-level simulation of gate_level_netlist.v (the hardened
#                          design, from the tt_submission artifact of a gds run) with the
#                          IHP cell and SRAM models under $(PDK_ROOT); in CI the gl_test
#                          job does this
#
# Results: results.xml (pass/fail per test), tb.fst (waveforms).
# Docs: https://docs.cocotb.org/en/stable/quickstart.html

SIM ?= icarus
FST ?= -fst
TOPLEVEL_LANG ?= verilog
SRC_DIR = $(PWD)/../src
# source_files in info.yaml, minus the memories
PROJECT_SOURCES = tt_um_theotacconi_openpio.v spi.v openpio.v openpio_sm.v pau.v fifo.v
# IHP SRAM macros (src/*_sram.v): behavioural models, compiled with -DFUNCTIONAL
SRAM_MODELS = $(addprefix $(PDK_ROOT)/ihp-sg13cmos5l/libs.ref/sg13cmos5l_sram/verilog/,\
	RM_IHPSG13_1P_64x16_c2.v RM_IHPSG13_1P_core_behavioral.v \
	RM_IHPSG13_1P_256x16_c2_bm_bist.v RM_IHPSG13_1P_core_behavioral_bm_bist.v)

ifneq ($(GATES),yes)

# RTL simulation:
SIM_BUILD				= sim_build/rtl
VERILOG_SOURCES += $(addprefix $(SRC_DIR)/,$(PROJECT_SOURCES))
ifneq ($(IMEM),sram)
VERILOG_SOURCES += $(SRC_DIR)/imem.v $(SRC_DIR)/fifo_ram.v
else
SIM_BUILD				= sim_build/rtl_sram
COMPILE_ARGS    += -DFUNCTIONAL
VERILOG_SOURCES += $(SRC_DIR)/imem_sram.v $(SRC_DIR)/fifo_ram_sram.v $(SRAM_MODELS)
endif

else

# Gate level simulation:
SIM_BUILD				= sim_build/gl
COMPILE_ARGS    += -DGL_TEST
COMPILE_ARGS    += -DFUNCTIONAL
COMPILE_ARGS    += -DSIM
VERILOG_SOURCES += $(PDK_ROOT)/ihp-sg13cmos5l/libs.ref/sg13cmos5l_io/verilog/sg13cmos5l_io.v
VERILOG_SOURCES += $(PDK_ROOT)/ihp-sg13cmos5l/libs.ref/sg13cmos5l_stdcell/verilog/sg13cmos5l_udp.v
VERILOG_SOURCES += $(PDK_ROOT)/ihp-sg13cmos5l/libs.ref/sg13cmos5l_stdcell/verilog/sg13cmos5l_stdcell.v
VERILOG_SOURCES += $(SRAM_MODELS)

# this gets copied in by the GDS action workflow
VERILOG_SOURCES += $(PWD)/gate_level_netlist.v

endif

COMPILE_ARGS 		+= -I$(SRC_DIR)

VERILOG_SOURCES += $(PWD)/tb.v
COCOTB_TOPLEVEL = tb
COCOTB_TEST_MODULES = test

include $(shell cocotb-config --makefiles)/Makefile.sim
