// tt_um_theotacconi_openpio.v - Tiny Tapeout top level of openPIO v2 (HOST.md, Pins and SPI).
//
//   ui_in[0] SCLK, ui_in[1] MOSI, ui_in[2] CS_N      uo_out[0] MISO, uo_out[1] HOST_IRQ
//   pin k = uio[k] (k < 8): bidirectional, direction latch, open drain
//   pin 8 + k = uo_out[k] (output) and ui_in[k] (input) for the pads the host does not use:
//   pin 10 output only (its input pad is CS_N), pins 11..15 output and input, pins 8, 9 none
//
// The chip core (openpio.v) is chip.py; its pin inputs pass its 2-flip-flop synchronizer, the
// SPI pins spi.v's. Each SM has an IMEM (256 x 16) and a FIFO SRAM (256 x 16): imem.v and
// fifo_ram.v here, the IHP macros in the ASIC build (imem_sram.v, fifo_ram_sram.v, same ports).

`default_nettype none

module tt_um_theotacconi_openpio (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,       // always 1 when powered
    input  wire       clk,
    input  wire       rst_n
);

    localparam N_SM = 2;

    wire        h_valid;
    wire [2:0]  h_op;
    wire [1:0]  h_sm;
    wire [7:0]  h_addr;
    wire [15:0] h_wdata, h_rdata, h_imem_rdata, h_status;
    wire        host_irq, miso;
    wire [15:0] pin_out, pin_oe;

    wire [8*N_SM-1:0]  imem_addr;
    wire [N_SM-1:0]    imem_we, ram_we, ram_re;
    wire [15:0]        imem_wdata;
    wire [16*N_SM-1:0] imem_rdata, ram_wdata, ram_rdata;
    wire [8*N_SM-1:0]  ram_addr;

    spi u_spi (
        .clk          (clk),
        .rst_n        (rst_n),
        .sclk         (ui_in[0]),
        .mosi         (ui_in[1]),
        .cs_n         (ui_in[2]),
        .miso         (miso),
        .h_valid      (h_valid),
        .h_op         (h_op),
        .h_sm         (h_sm),
        .h_addr       (h_addr),
        .h_wdata      (h_wdata),
        .h_rdata      (h_rdata),
        .h_imem_rdata (h_imem_rdata),
        .h_status     (h_status)
    );

    openpio #(.N_SM(N_SM)) u_chip (
        .clk          (clk),
        .rst_n        (rst_n),
        .h_valid      (h_valid),
        .h_op         (h_op),
        .h_sm         (h_sm),
        .h_addr       (h_addr),
        .h_wdata      (h_wdata),
        .h_rdata      (h_rdata),
        .h_imem_rdata (h_imem_rdata),
        .h_status     (h_status),
        .host_irq     (host_irq),
        .pin_in       ({ui_in[7:3], 3'b000, uio_in}),
        .pin_out      (pin_out),
        .pin_oe       (pin_oe),
        .imem_addr    (imem_addr),
        .imem_we      (imem_we),
        .imem_wdata   (imem_wdata),
        .imem_rdata   (imem_rdata),
        .ram_addr     (ram_addr),
        .ram_we       (ram_we),
        .ram_re       (ram_re),
        .ram_wdata    (ram_wdata),
        .ram_rdata    (ram_rdata)
    );

    // one IMEM and one FIFO SRAM an SM, named for the macro placement (src/config.json)
    imem u_imem0 (.clk(clk), .addr(imem_addr[7:0]), .we(imem_we[0]), .wdata(imem_wdata),
                  .rdata(imem_rdata[15:0]));
    imem u_imem1 (.clk(clk), .addr(imem_addr[15:8]), .we(imem_we[1]), .wdata(imem_wdata),
                  .rdata(imem_rdata[31:16]));
    fifo_ram u_ram0 (.clk(clk), .addr(ram_addr[7:0]), .we(ram_we[0]), .re(ram_re[0]),
                     .wdata(ram_wdata[15:0]), .rdata(ram_rdata[15:0]));
    fifo_ram u_ram1 (.clk(clk), .addr(ram_addr[15:8]), .we(ram_we[1]), .re(ram_re[1]),
                     .wdata(ram_wdata[31:16]), .rdata(ram_rdata[31:16]));

    assign uo_out  = {pin_out[15:10], host_irq, miso};
    assign uio_out = pin_out[7:0];
    assign uio_oe  = pin_oe[7:0];

    wire _unused = &{ena, pin_out[9:8], pin_oe[15:8], 1'b0};

endmodule

`default_nettype wire
