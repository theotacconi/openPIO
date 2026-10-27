// sram_blackbox.v - port declaration of the IHP SRAM macro used by imem_sram.v
//
// For the LibreLane flow only (info.yaml source_files): its Verilator lint
// needs every instantiated module declared, and Yosys keeps the macro as a
// black box (timing from the PDK Liberty files in src/config.json MACROS).
// Simulation uses IHP's behavioural model instead (test/Makefile).

`default_nettype none

(* blackbox *)
module RM_IHPSG13_1P_64x16_c2 (
    input  wire        A_CLK,
    input  wire        A_MEN,
    input  wire        A_WEN,
    input  wire        A_REN,
    input  wire [5:0]  A_ADDR,
    input  wire [15:0] A_DIN,
    input  wire        A_DLY,
    output wire [15:0] A_DOUT
);
endmodule

`default_nettype wire
