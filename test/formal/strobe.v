// strobe.v - proves the FORMAL assertion in src/openpio_sm.v: the strobe registered one clock
// ahead equals the emu.py prescaler rule on the current state, for every input sequence after
// a chip reset (every instruction, host write, CLKDIV and fraction). Run: sby -f strobe.sby

`default_nettype none

module strobe (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        en,
    input  wire        run,
    input  wire        reset,
    input  wire [15:0] instr,
    input  wire        h_we,
    input  wire [5:0]  h_addr,
    input  wire [15:0] h_wdata,
    input  wire [15:0] gpio_in,
    input  wire [15:0] latch_out,
    input  wire [15:0] latch_dir,
    input  wire        tx_empty,
    input  wire [15:0] tx_data,
    input  wire        rx_full,
    input  wire        tx_fifo_empty,
    input  wire        rx_fifo_full,
    input  wire [7:0]  peer_irq,
    input  wire        in_we,
    input  wire [2:0]  in_idx,
    input  wire        in_val
);

    openpio_sm u_sm (
        .clk (clk), .rst_n (rst_n), .en (en), .run (run), .reset (reset), .strobe (),
        .imem_addr (), .instr (instr),
        .h_we (h_we), .h_addr (h_addr), .h_wdata (h_wdata), .h_rdata (),
        .gpio_in (gpio_in), .latch_out (latch_out), .latch_dir (latch_dir),
        .out_we (), .out_val (), .dir_we (), .dir_val (), .pau_we (), .pau_val (),
        .tx_empty (tx_empty), .tx_data (tx_data), .tx_pop (),
        .rx_full (rx_full), .rx_push (), .rx_data (),
        .tx_fifo_empty (tx_fifo_empty), .rx_fifo_full (rx_fifo_full),
        .irq (), .peer_irq (peer_irq), .peer_we (), .peer_idx (), .peer_val (),
        .in_we (in_we), .in_idx (in_idx), .in_val (in_val),
        .irq_mask (), .pc (), .stalled ()
    );

    // the first clock is a chip reset
    reg init = 1'b1;
    always @(posedge clk) init <= 1'b0;
    always @(*) if (init) assume (!rst_n);

endmodule

`default_nettype wire
