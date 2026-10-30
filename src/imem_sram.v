// imem_sram.v - an SM's instruction memory on the IHP SRAM macro (ASIC build only, info.yaml):
// imem.v with the same module name, ports and timing. imem.v stays the model for RTL
// simulation, lockstep, formal and the FPGA; the gate-level test simulates IHP's model.
//
// RM_IHPSG13_1P_256x16_c2_bm_bist (libs.ref/sg13g2_sram), 256 x 16, one port, the FIFO SRAM's
// macro; every bit written (A_BM all ones), BIST off:
//   A_WEN: mem[A_ADDR] <= A_DIN, A_DOUT holds;  A_REN: A_DOUT <= mem[A_ADDR] at the edge.
// Slow corner: A_CLK -> A_DOUT about 5.0 ns (as the 64 x 16 of ISA v2), the start of the SM's
// critical path.

`default_nettype none

module imem (
    input  wire        clk,
    input  wire [7:0]  addr,
    input  wire        we,
    input  wire [15:0] wdata,
    output wire [15:0] rdata
);

    (* keep *)
    RM_IHPSG13_1P_256x16_c2_bm_bist u_sram (
        .A_CLK       (clk),
        .A_MEN       (1'b1),
        .A_WEN       (we),
        .A_REN       (~we),
        .A_ADDR      (addr),
        .A_DIN       (wdata),
        .A_DLY       (1'b1),            // must be 1 (IHP model stops otherwise)
        .A_DOUT      (rdata),
        .A_BM        (16'hFFFF),
        .A_BIST_CLK  (1'b0),
        .A_BIST_EN   (1'b0),
        .A_BIST_MEN  (1'b0),
        .A_BIST_WEN  (1'b0),
        .A_BIST_REN  (1'b0),
        .A_BIST_ADDR (8'd0),
        .A_BIST_DIN  (16'd0),
        .A_BIST_BM   (16'd0)
    );

endmodule

`default_nettype wire
