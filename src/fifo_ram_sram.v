// fifo_ram_sram.v - an SM's FIFO data memory on the IHP SRAM macro (ASIC build only, info.yaml):
// fifo_ram.v with the same module name, ports and timing.
//
// RM_IHPSG13_1P_256x16_c2_bm_bist (libs.ref/sg13g2_sram), 256 x 16, one port; every bit written
// (A_BM all ones), BIST off. Enabled only for an access: idle clocks leave the array alone.

`default_nettype none

module fifo_ram (
    input  wire        clk,
    input  wire [7:0]  addr,
    input  wire        we,
    input  wire        re,
    input  wire [15:0] wdata,
    output wire [15:0] rdata
);

    (* keep *)
    RM_IHPSG13_1P_256x16_c2_bm_bist u_sram (
        .A_CLK       (clk),
        .A_MEN       (we | re),
        .A_WEN       (we),
        .A_REN       (re & ~we),
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
