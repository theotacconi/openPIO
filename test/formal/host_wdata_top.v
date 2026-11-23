// host_wdata_top.v - the two-clock multicycle path src/signoff.sdc declares from the 16
// flip-flops behind the host's write data (spi.v w = {sr, s_mosi[1]}), proved on the RTL: in
// the clock after one of them changes, whatever reads it (the SPI's own decode, the core, the
// SRAM inputs) may see any value in its place, and the chip behaves the same.
//
// top_gold is the SPI slave and the core as built (src/tt_um_theotacconi_openpio.v without the
// SRAMs, whose ports are outputs and inputs here). top_gate is the same with spi_seen, a copy of
// spi.v made by host_wdata_top.ys in which every reader of w sees, for each bit that changed at
// the last clock edge, a free input (garbage) instead. The two are proved equivalent, register for
// register and output for output. The SRAMs read A_DIN only when written and A_ADDR only when
// enabled, so those outputs are compared then; the IMEM is enabled every clock.
//
// What the SPI master may do: anything, except change MOSI while SCLK is high (SPI mode 0
// changes it after the falling edge); asynchronous inputs are values the clock samples.
// Run: yosys -q -s host_wdata_top.ys (about 6 minutes). Checked by mutation: MOSI allowed to
// change while SCLK is high, the CTRL write's stepping bits taken in every clock, the IMEM data
// compared when it is not written: each fails.

`default_nettype none

module top #(parameter SEEN = 0) (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        sclk,
    input  wire        mosi_in,
    input  wire        cs_n,
    input  wire [15:0] garbage,
    output wire        miso,
    input  wire [15:0] pin_in,
    output wire [15:0] pin_out,
    output wire [15:0] pin_oe,
    output wire        host_irq,
    output wire [15:0] imem_addr,
    output wire [1:0]  imem_we,
    output wire [15:0] imem_wdata_used,
    input  wire [31:0] imem_rdata,
    output wire [15:0] ram_addr_used,
    output wire [1:0]  ram_we,
    output wire [1:0]  ram_re,
    output wire [31:0] ram_wdata_used,
    input  wire [31:0] ram_rdata
);

    // MOSI holds while SCLK is high
    reg  mosi_q;
    wire mosi = sclk ? mosi_q : mosi_in;
    always @(posedge clk)
        mosi_q <= mosi;

    wire        h_valid;
    wire [2:0]  h_op;
    wire [1:0]  h_sm;
    wire [7:0]  h_addr;
    wire [15:0] h_wdata, h_rdata, h_imem_rdata, h_status;
    wire [15:0] imem_wdata, ram_addr;
    wire [31:0] ram_wdata;

    generate
        if (SEEN) begin : g_seen
            spi_seen u_spi (
                .garbage(garbage),
                .clk(clk), .rst_n(rst_n), .sclk(sclk), .mosi(mosi), .cs_n(cs_n), .miso(miso),
                .h_valid(h_valid), .h_op(h_op), .h_sm(h_sm), .h_addr(h_addr), .h_wdata(h_wdata),
                .h_rdata(h_rdata), .h_imem_rdata(h_imem_rdata), .h_status(h_status)
            );
        end else begin : g_seen
            spi u_spi (
                .clk(clk), .rst_n(rst_n), .sclk(sclk), .mosi(mosi), .cs_n(cs_n), .miso(miso),
                .h_valid(h_valid), .h_op(h_op), .h_sm(h_sm), .h_addr(h_addr), .h_wdata(h_wdata),
                .h_rdata(h_rdata), .h_imem_rdata(h_imem_rdata), .h_status(h_status)
            );
        end
    endgenerate

    openpio #(.N_SM(2)) u_chip (
        .clk(clk), .rst_n(rst_n),
        .h_valid(h_valid), .h_op(h_op), .h_sm(h_sm), .h_addr(h_addr), .h_wdata(h_wdata),
        .h_rdata(h_rdata), .h_imem_rdata(h_imem_rdata), .h_status(h_status),
        .host_irq(host_irq),
        .pin_in(pin_in), .pin_out(pin_out), .pin_oe(pin_oe),
        .imem_addr(imem_addr), .imem_we(imem_we), .imem_wdata(imem_wdata),
        .imem_rdata(imem_rdata),
        .ram_addr(ram_addr), .ram_we(ram_we), .ram_re(ram_re), .ram_wdata(ram_wdata),
        .ram_rdata(ram_rdata)
    );

    assign imem_wdata_used = |imem_we ? imem_wdata : 16'd0;
    genvar i;
    for (i = 0; i < 2; i = i + 1) begin : g_ram
        assign ram_addr_used[8*i +: 8]    = (ram_we[i] | ram_re[i]) ? ram_addr[8*i +: 8] : 8'd0;
        assign ram_wdata_used[16*i +: 16] = ram_we[i] ? ram_wdata[16*i +: 16] : 16'd0;
    end

endmodule

module top_gold (
    input  wire        clk, rst_n, sclk, mosi_in, cs_n,
    input  wire [15:0] garbage, pin_in,
    input  wire [31:0] imem_rdata, ram_rdata,
    output wire        miso, host_irq,
    output wire [15:0] pin_out, pin_oe, imem_addr, imem_wdata_used, ram_addr_used,
    output wire [31:0] ram_wdata_used,
    output wire [1:0]  imem_we, ram_we, ram_re
);
    top #(.SEEN(0)) u (.*);
endmodule

module top_gate (
    input  wire        clk, rst_n, sclk, mosi_in, cs_n,
    input  wire [15:0] garbage, pin_in,
    input  wire [31:0] imem_rdata, ram_rdata,
    output wire        miso, host_irq,
    output wire [15:0] pin_out, pin_oe, imem_addr, imem_wdata_used, ram_addr_used,
    output wire [31:0] ram_wdata_used,
    output wire [1:0]  imem_we, ram_we, ram_re
);
    top #(.SEEN(1)) u (.*);
endmodule
