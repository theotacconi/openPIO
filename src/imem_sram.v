// imem_sram.v - an SM's instruction memory on the IHP SRAM macro (ASIC build only, info.yaml):
// imem.v with the same module name, ports and timing. imem.v stays the model for RTL
// simulation, lockstep, formal and the FPGA; the gate-level test simulates IHP's model.
//
// RM_IHPSG13_1P_64x16_c2 (libs.ref/sg13g2_sram), 64 x 16, one port:
//   A_WEN: mem[A_ADDR] <= A_DIN, A_DOUT holds;  A_REN: A_DOUT <= mem[A_ADDR] at the edge.
// Slow corner: A_CLK -> A_DOUT up to 5.3 ns, the start of the SM's critical path.

`default_nettype none

module imem (
    input  wire        clk,
    input  wire [5:0]  addr,
    input  wire        we,
    input  wire [15:0] wdata,
    output wire [15:0] rdata
);

    (* keep *)
    RM_IHPSG13_1P_64x16_c2 u_sram (
        .A_CLK  (clk),
        .A_MEN  (1'b1),
        .A_WEN  (we),
        .A_REN  (~we),
        .A_ADDR (addr),
        .A_DIN  (wdata),
        .A_DLY  (1'b1),                 // must be 1 (IHP model stops otherwise)
        .A_DOUT (rdata)
    );

endmodule

`default_nettype wire
