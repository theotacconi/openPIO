// top_tb.v - the Tiny Tapeout top for test/lockstep/top_lockstep.py: its pads, and the chip
// core's host access (hierarchical references) so the model makes the same access each clock.

`default_nettype none

module top_tb (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [7:0]  ui_in,
    input  wire [7:0]  uio_in,
    output wire [7:0]  uo_out,
    output wire [7:0]  uio_out,
    output wire [7:0]  uio_oe,
    output wire        h_valid,
    output wire [2:0]  h_op,
    output wire [1:0]  h_sm,
    output wire [7:0]  h_addr,
    output wire [15:0] h_wdata
);

    tt_um_theotacconi_openpio u_top (
        .ui_in   (ui_in),
        .uo_out  (uo_out),
        .uio_in  (uio_in),
        .uio_out (uio_out),
        .uio_oe  (uio_oe),
        .ena     (1'b1),
        .clk     (clk),
        .rst_n   (rst_n)
    );

    assign h_valid = u_top.h_valid;
    assign h_op    = u_top.h_op;
    assign h_sm    = u_top.h_sm;
    assign h_addr  = u_top.h_addr;
    assign h_wdata = u_top.h_wdata;

endmodule

`default_nettype wire
