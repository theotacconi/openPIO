// tang_top.v - Tang Nano 20K (GW2AR-LV18QN88C8/I7) prototype of the openPIO
// Tiny Tapeout design. FPGA-only wrapper: src/ is used unmodified.
//
// clk     27 MHz crystal, used directly (TT target is 50 MHz; this runs
//         slower, so SPI SCLK <= 27 MHz / 12 = 2.25 MHz)
// reset   S1 (pin 88) reads 1 while pressed (Sipeed examples invert it);
//         2-FF synchronized, OR'ed with a 16-clk power-on reset
// SPI     host bridge on J5/J6 header pins (see tang_nano_20k.cst)
// pio     uio[7:0] as true bidirectional pads: oe = pindirs; the pad value
//         is read back, so open-drain buses work with pull-ups
// LEDs    active low: [0] HOST_IRQ  [1] SPI frame (stretched)
//         [2] pio input activity (stretched)  [3] any pio driven
//         [4] reset active  [5] heartbeat (~0.8 Hz)
// WS2812  held low (off)
//
// FIFO flags are not on the TT pins; read them via SPI STATUS (reg 8).

`default_nettype none

module tang_top (
    input  wire       clk_27m,
    input  wire       btn_s1,
    input  wire       spi_sclk,
    input  wire       spi_mosi,
    input  wire       spi_cs_n,
    output wire       spi_miso,
    output wire       host_irq,
    inout  wire [7:0] pio,
    output wire [5:0] led_n,
    output wire       ws2812
);

    // ---------------------------------------------------------------
    // Reset: power-on counter (FPGA init values) + synchronized S1
    // ---------------------------------------------------------------
    reg [3:0] por_cnt = 4'd0;
    reg [1:0] btn_q   = 2'b00;
    reg       rst_n   = 1'b0;

    always @(posedge clk_27m) begin
        if (por_cnt != 4'hF)
            por_cnt <= por_cnt + 4'd1;
        btn_q <= {btn_q[0], btn_s1};
        rst_n <= (por_cnt == 4'hF) & ~btn_q[1];
    end

    // ---------------------------------------------------------------
    // Tiny Tapeout design
    // ---------------------------------------------------------------
    wire [7:0] uo_out, uio_out, uio_oe;
    wire [7:0] uio_in = pio;

    tt_um_theotacconi_openpio u_tt (
        .ui_in   ({5'b00000, spi_cs_n, spi_mosi, spi_sclk}),
        .uo_out  (uo_out),
        .uio_in  (uio_in),
        .uio_out (uio_out),
        .uio_oe  (uio_oe),
        .ena     (1'b1),
        .clk     (clk_27m),
        .rst_n   (rst_n)
    );

    genvar i;
    generate
        for (i = 0; i < 8; i = i + 1) begin : g_pio
            assign pio[i] = uio_oe[i] ? uio_out[i] : 1'bz;
        end
    endgenerate

    assign spi_miso = uo_out[0];
    assign host_irq = uo_out[1];

    // ---------------------------------------------------------------
    // Status LEDs
    // ---------------------------------------------------------------
    reg  [1:0]  cs_q;
    reg  [7:0]  pio_q1, pio_q2;
    reg  [21:0] cs_stretch, pio_stretch;   // 2^22 / 27 MHz = 155 ms
    reg  [24:0] beat;

    always @(posedge clk_27m) begin
        if (!rst_n) begin
            cs_q        <= 2'b11;
            pio_q1      <= 8'd0;
            pio_q2      <= 8'd0;
            cs_stretch  <= 22'd0;
            pio_stretch <= 22'd0;
            beat        <= 25'd0;
        end else begin
            cs_q   <= {cs_q[0], spi_cs_n};
            pio_q1 <= pio;
            pio_q2 <= pio_q1;
            beat   <= beat + 25'd1;
            if (!cs_q[1])
                cs_stretch <= {22{1'b1}};
            else if (cs_stretch != 22'd0)
                cs_stretch <= cs_stretch - 22'd1;
            if (pio_q1 != pio_q2)
                pio_stretch <= {22{1'b1}};
            else if (pio_stretch != 22'd0)
                pio_stretch <= pio_stretch - 22'd1;
        end
    end

    assign led_n = ~{beat[24], ~rst_n, |uio_oe, |pio_stretch, |cs_stretch, host_irq};
    assign ws2812 = 1'b0;

endmodule

`default_nettype wire
