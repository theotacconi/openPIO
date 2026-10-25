// core_strobe.v - proves the FORMAL assertion in src/openpio_core.v: the
// registered strobe equals the emu.py step-0 rule on the current prescaler
// state, for every input sequence after reset (all inputs free, including
// en right after reset). Run: sby -f core_strobe.sby

`default_nettype none

module core_strobe (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        en,
    input  wire        imem_wr_en,
    input  wire [4:0]  imem_wr_addr,
    input  wire [15:0] imem_wr_data,
    input  wire [4:0]  cfg_wrap_bottom,
    input  wire [4:0]  cfg_wrap_top,
    input  wire [2:0]  cfg_ss_count,
    input  wire        cfg_ss_opt,
    input  wire        cfg_ss_pindirs,
    input  wire [2:0]  cfg_ss_base,
    input  wire [2:0]  cfg_in_base,
    input  wire [2:0]  cfg_out_base,
    input  wire [2:0]  cfg_set_base,
    input  wire [2:0]  cfg_set_count,
    input  wire [2:0]  cfg_jmp_pin,
    input  wire [7:0]  cfg_od_mask,
    input  wire        cfg_in_shr,
    input  wire        cfg_out_shr,
    input  wire [4:0]  cfg_clkdiv_int,
    input  wire [7:0]  cfg_clkdiv_frac,
    input  wire [7:0]  gpio_in,
    input  wire        tx_empty,
    input  wire [15:0] tx_data,
    input  wire        rx_full,
    input  wire [7:0]  irq_clr,
    input  wire [7:0]  peer_irq_clr
);

    openpio_core u_core (
        .clk              (clk),
        .rst_n            (rst_n),
        .en               (en),
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
        .gpio_in          (gpio_in),
        .gpio_out         (),
        .gpio_oe          (),
        .tx_empty         (tx_empty),
        .tx_data          (tx_data),
        .tx_pop           (),
        .rx_full          (rx_full),
        .rx_data          (),
        .rx_push          (),
        .irq_clr          (irq_clr),
        .peer_irq_clr     (peer_irq_clr),
        .irq_flags        (),
        .peer_irq_flags   (),
        .dbg_pc           (),
        .dbg_instr        (),
        .dbg_x            (),
        .dbg_y            (),
        .dbg_isr          (),
        .dbg_osr          (),
        .dbg_isr_cnt      (),
        .dbg_osr_cnt      (),
        .dbg_delay        (),
        .dbg_stalled      (),
        .dbg_clkdiv       (),
        .dbg_timeout      (),
        .dbg_timeout_flag (),
        .dbg_pau_cfg      (),
        .dbg_pau_baud     (),
        .dbg_mtx_nsym     (),
        .dbg_nrzi_k       (),
        .dbg_nrzi_ones    ()
    );

    // First cycle is a reset; the core asserts its invariant from then on.
    reg init = 1'b1;
    always @(posedge clk) init <= 1'b0;
    always @(*) if (init) assume (!rst_n);

endmodule

`default_nettype wire
