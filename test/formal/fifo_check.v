// fifo_check.v - proves the FORMAL properties of src/fifo.v: the TX and RX FIFOs pass every
// word once, in order, unchanged, for every host and SM sequence after a chip reset (CTRL.RESET
// too) and any SRAM that keeps its contract (a free ram_rdata, constrained in fifo.v).
// Run: sby -f fifo.sby

`default_nettype none

module fifo_check (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        reset,
    input  wire        tx_pop,
    input  wire        rx_push,
    input  wire [15:0] rx_data,
    input  wire        h_push,
    input  wire [15:0] h_wdata,
    input  wire        h_pop,
    input  wire [15:0] ram_rdata
);

    fifo u_fifo (
        .clk (clk), .rst_n (rst_n), .reset (reset),
        .tx_empty (), .tx_data (), .tx_pop (tx_pop),
        .rx_full (), .rx_push (rx_push), .rx_data (rx_data),
        .tx_level (), .rx_level (),
        .h_push (h_push), .h_wdata (h_wdata), .h_room (), .h_pop (h_pop), .h_rdata (),
        .ram_addr (), .ram_we (), .ram_re (), .ram_wdata (), .ram_rdata (ram_rdata)
    );

    // the first clock is a chip reset
    reg init = 1'b1;
    always @(posedge clk) init <= 1'b0;
    always @(*) if (init) assume (!rst_n);

endmodule

`default_nettype wire
