// tt_um_openpio.v - Tiny Tapeout top level for openPIO
//
// Pins:
//   ui_in[0]  SPI SCLK      ui_in[1] SPI MOSI      ui_in[2] SPI CS_n
//   ui_in[7:3]  alternate gpio_in[7:3] sources (GPIO reg in_sel)
//   uo_out[0] SPI MISO      uo_out[1] HOST_IRQ = |irq_flags
//   uo_out[7:2] gpio_out[7:2] (push-pull mirror)
//   uio[7:0]  openPIO GPIO: uio_out = gpio_out, uio_oe = pindirs,
//             gpio_in = uio_in (pad readback, so open-drain lines see the bus)
//
// gpio_in passes a 2-FF synchronizer: the SM sees pins 2 clk late.
// CTRL.RESET resets the SM, its IMEM read port and both FIFOs (emu.py
// reset()); config registers and IMEM contents survive. EN reaches the
// core one clk late, so the core always gets the en = 0 cycle it needs
// after reset to fetch mem[0].

`default_nettype none

module tt_um_theotacconi_openpio (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,       // always 1 when powered; unused
    input  wire       clk,
    input  wire       rst_n
);

    // ---------------------------------------------------------------
    // Host interface
    // ---------------------------------------------------------------
    wire        spi_miso;
    wire        imem_wr_en;
    wire [4:0]  imem_wr_addr;
    wire [15:0] imem_wr_data;
    wire        sm_en, sm_reset;
    wire [4:0]  cfg_wrap_bottom, cfg_wrap_top, cfg_clkdiv_int;
    wire [2:0]  cfg_ss_count, cfg_ss_base, cfg_in_base, cfg_out_base;
    wire [2:0]  cfg_set_base, cfg_set_count, cfg_jmp_pin;
    wire        cfg_ss_opt, cfg_ss_pindirs, cfg_in_shr, cfg_out_shr;
    wire [7:0]  cfg_clkdiv_frac, cfg_od_mask;
    wire [7:3]  cfg_in_sel;
    wire        tx_push, rx_pop;
    wire [15:0] tx_wdata;
    wire [7:0]  irq_clr, peer_irq_clr;

    wire        tx_empty, tx_full, rx_empty, rx_full;
    wire [15:0] tx_rdata, rx_rdata;
    wire        tx_pop, rx_push;
    wire [15:0] rx_wdata;
    wire [7:0]  irq_flags, peer_irq_flags;
    wire [7:0]  gpio_out, gpio_oe;
    wire [4:0]  sm_pc;
    wire [15:0] sm_x, sm_y, sm_isr, sm_osr;
    wire        sm_stalled, sm_timeout_flag;

    spi_cfg_interface u_spi (
        .clk             (clk),
        .rst_n           (rst_n),
        .spi_sclk        (ui_in[0]),
        .spi_mosi        (ui_in[1]),
        .spi_cs_n        (ui_in[2]),
        .spi_miso        (spi_miso),
        .imem_wr_en      (imem_wr_en),
        .imem_wr_addr    (imem_wr_addr),
        .imem_wr_data    (imem_wr_data),
        .sm_en           (sm_en),
        .sm_reset        (sm_reset),
        .cfg_wrap_bottom (cfg_wrap_bottom),
        .cfg_wrap_top    (cfg_wrap_top),
        .cfg_ss_count    (cfg_ss_count),
        .cfg_ss_opt      (cfg_ss_opt),
        .cfg_ss_pindirs  (cfg_ss_pindirs),
        .cfg_ss_base     (cfg_ss_base),
        .cfg_in_base     (cfg_in_base),
        .cfg_out_base    (cfg_out_base),
        .cfg_set_base    (cfg_set_base),
        .cfg_set_count   (cfg_set_count),
        .cfg_jmp_pin     (cfg_jmp_pin),
        .cfg_in_shr      (cfg_in_shr),
        .cfg_out_shr     (cfg_out_shr),
        .cfg_clkdiv_int  (cfg_clkdiv_int),
        .cfg_clkdiv_frac (cfg_clkdiv_frac),
        .cfg_od_mask     (cfg_od_mask),
        .cfg_in_sel      (cfg_in_sel),
        .tx_push         (tx_push),
        .tx_wdata        (tx_wdata),
        .tx_empty        (tx_empty),
        .tx_full         (tx_full),
        .rx_pop          (rx_pop),
        .rx_rdata        (rx_rdata),
        .rx_empty        (rx_empty),
        .rx_full         (rx_full),
        .irq_flags       (irq_flags),
        .peer_irq_flags  (peer_irq_flags),
        .irq_clr         (irq_clr),
        .peer_irq_clr    (peer_irq_clr),
        .sm_pc           (sm_pc),
        .sm_stalled      (sm_stalled),
        .sm_timeout_flag (sm_timeout_flag),
        .sm_x            (sm_x),
        .sm_y            (sm_y),
        .sm_isr          (sm_isr),
        .sm_osr          (sm_osr),
        .sm_gpio_out     (gpio_out),
        .sm_gpio_oe      (gpio_oe)
    );

    // ---------------------------------------------------------------
    // SM reset domain, run enable, GPIO input path
    // ---------------------------------------------------------------
    wire core_rst_n = rst_n & ~sm_reset;

    reg       core_en;
    reg [7:0] gpio_sync1, gpio_sync2;

    wire [7:0] gpio_pad = {(cfg_in_sel & ui_in[7:3]) | (~cfg_in_sel & uio_in[7:3]), uio_in[2:0]};

    always @(posedge clk) begin
        if (!core_rst_n)
            core_en <= 1'b0;
        else
            core_en <= sm_en;
    end

    always @(posedge clk) begin
        if (!rst_n) begin
            gpio_sync1 <= 8'd0;
            gpio_sync2 <= 8'd0;
        end else begin
            gpio_sync1 <= gpio_pad;
            gpio_sync2 <= gpio_sync1;
        end
    end

    // ---------------------------------------------------------------
    // FIFOs: TX host -> SM, RX SM -> host
    // ---------------------------------------------------------------
    fifo u_tx_fifo (
        .clk   (clk),
        .rst_n (core_rst_n),
        .push  (tx_push),
        .wdata (tx_wdata),
        .pop   (tx_pop),
        .rdata (tx_rdata),
        .empty (tx_empty),
        .full  (tx_full)
    );

    fifo u_rx_fifo (
        .clk   (clk),
        .rst_n (core_rst_n),
        .push  (rx_push),
        .wdata (rx_wdata),
        .pop   (rx_pop),
        .rdata (rx_rdata),
        .empty (rx_empty),
        .full  (rx_full)
    );

    // ---------------------------------------------------------------
    // State machine
    // ---------------------------------------------------------------
    openpio_core u_core (
        .clk              (clk),
        .rst_n            (core_rst_n),
        .en               (core_en),
        .imem_wr_en       (imem_wr_en),
        .imem_wr_addr     (imem_wr_addr),
        .imem_wr_data     (imem_wr_data),
        .cfg_wrap_bottom  (cfg_wrap_bottom),
        .cfg_wrap_top     (cfg_wrap_top),
        .cfg_ss_count     (cfg_ss_count),
        .cfg_ss_opt       (cfg_ss_opt),
        .cfg_ss_pindirs   (cfg_ss_pindirs),
        .cfg_ss_base      (cfg_ss_base),
        .cfg_in_base      (cfg_in_base),
        .cfg_out_base     (cfg_out_base),
        .cfg_set_base     (cfg_set_base),
        .cfg_set_count    (cfg_set_count),
        .cfg_jmp_pin      (cfg_jmp_pin),
        .cfg_od_mask      (cfg_od_mask),
        .cfg_in_shr       (cfg_in_shr),
        .cfg_out_shr      (cfg_out_shr),
        .cfg_clkdiv_int   (cfg_clkdiv_int),
        .cfg_clkdiv_frac  (cfg_clkdiv_frac),
        .gpio_in          (gpio_sync2),
        .gpio_out         (gpio_out),
        .gpio_oe          (gpio_oe),
        .tx_empty         (tx_empty),
        .tx_data          (tx_rdata),
        .tx_pop           (tx_pop),
        .rx_full          (rx_full),
        .rx_data          (rx_wdata),
        .rx_push          (rx_push),
        .irq_clr          (irq_clr),
        .peer_irq_clr     (peer_irq_clr),
        .irq_flags        (irq_flags),
        .peer_irq_flags   (peer_irq_flags),
        .dbg_pc           (sm_pc),
        .dbg_instr        (),
        .dbg_x            (sm_x),
        .dbg_y            (sm_y),
        .dbg_isr          (sm_isr),
        .dbg_osr          (sm_osr),
        .dbg_isr_cnt      (),
        .dbg_osr_cnt      (),
        .dbg_delay        (),
        .dbg_stalled      (sm_stalled),
        .dbg_clkdiv       (),
        .dbg_timeout      (),
        .dbg_timeout_flag (sm_timeout_flag),
        .dbg_pau_cfg      (),
        .dbg_pau_baud     (),
        .dbg_mtx_nsym     (),
        .dbg_nrzi_k       (),
        .dbg_nrzi_ones    ()
    );

    // ---------------------------------------------------------------
    // Outputs
    // ---------------------------------------------------------------
    assign uo_out  = {gpio_out[7:2], |irq_flags, spi_miso};
    assign uio_out = gpio_out;
    assign uio_oe  = gpio_oe;

    wire _unused = &{ena, 1'b0};

endmodule

`default_nettype wire
