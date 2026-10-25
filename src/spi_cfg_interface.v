// spi_cfg_interface.v - openPIO host SPI slave + control/config registers
//
// SPI mode 0 (CPOL=0, CPHA=0), MSB first. SCLK/MOSI/CS_n are 2-FF
// synchronized into clk and edge-detected, so there is one clock domain:
// SCLK high and low phases must each last >= 6 clk cycles (f_SCLK <=
// f_clk / 12, ~4 MHz at 50 MHz).
//
// Frame = 24 bits while CS_n is low: cmd[7:0] then data[15:0]. Frames may
// run back-to-back under one CS_n; raising CS_n aborts a partial frame.
//   cmd = {op[2:0], addr[4:0]}
//   op 001 IMEM_WR  imem[addr] <= data                       (at bit 24)
//   op 010 REG_WR   reg[addr]  <= data                       (at bit 24)
//   op 011 REG_RD   MISO data  = reg[addr]                   (latched at bit 8)
//   op 100 TX_PUSH  TX FIFO    <= data, dropped if full      (at bit 24)
//   op 101 RX_POP   MISO data  = RX head, popped; 0 if empty (at bit 8)
//   op 110 IRQ      MISO data  = {peer_irq, irq} (bit 8); data bits set
//                   clear those flags, W1C (bit 24)
//   op 000/111      no operation
// For every op except REG_RD / RX_POP / IRQ, MISO data = STATUS.
// MISO is 0 during the command byte; data bit 15 is valid before SCLK
// rise 9, later bits change after each falling edge.
//
// Registers (reset values = emu.py StateMachine defaults):
//   0 CTRL     [0] EN  [1] RESET (held: SM, IMEM read port, FIFOs in reset)
//   1 WRAP     [4:0] wrap_bottom=0  [12:8] wrap_top=31
//   2 SIDESET  [2:0] count  [3] opt  [4] pindirs  [10:8] base
//   3 PINBASE  [2:0] in_base  [6:4] out_base  [10:8] set_base  [14:12] set_count=5
//   4 MISC     [2:0] jmp_pin  [4] in_shift_right=1  [5] out_shift_right=1
//   5 CLKDIV   [4:0] int=1  [15:8] frac   (both loaded into the SM on RESET)
//   6 GPIO     [7:0] open_drain_mask  [15:11] in_sel[7:3] (1: gpio_in[i] = ui_in[i])
//   8 STATUS   [0] tx_empty [1] tx_full [2] rx_empty [3] rx_full [4] stalled
//              [5] timeout_flag [12:8] pc                                  (RO)
//   9 X  10 Y  11 ISR  12 OSR                                              (RO)
//  13 PINS     [7:0] gpio_out  [15:8] pindirs                              (RO)
// Unmapped addresses read 0; writes to RO / unmapped addresses are ignored.

`default_nettype none

module spi_cfg_interface (
    input  wire        clk,
    input  wire        rst_n,            // synchronous, active-low

    // SPI pins (asynchronous to clk)
    input  wire        spi_sclk,
    input  wire        spi_mosi,
    input  wire        spi_cs_n,
    output reg         spi_miso,

    // IMEM programming port
    output reg         imem_wr_en,
    output wire [4:0]  imem_wr_addr,
    output wire [15:0] imem_wr_data,

    // Control / configuration
    output reg         sm_en,
    output reg         sm_reset,
    output reg  [4:0]  cfg_wrap_bottom,
    output reg  [4:0]  cfg_wrap_top,
    output reg  [2:0]  cfg_ss_count,
    output reg         cfg_ss_opt,
    output reg         cfg_ss_pindirs,
    output reg  [2:0]  cfg_ss_base,
    output reg  [2:0]  cfg_in_base,
    output reg  [2:0]  cfg_out_base,
    output reg  [2:0]  cfg_set_base,
    output reg  [2:0]  cfg_set_count,
    output reg  [2:0]  cfg_jmp_pin,
    output reg         cfg_in_shr,
    output reg         cfg_out_shr,
    output reg  [4:0]  cfg_clkdiv_int,
    output reg  [7:0]  cfg_clkdiv_frac,
    output reg  [7:0]  cfg_od_mask,
    output reg  [7:3]  cfg_in_sel,

    // TX FIFO write side, RX FIFO read side
    output reg         tx_push,
    output wire [15:0] tx_wdata,
    input  wire        tx_empty,
    input  wire        tx_full,
    output reg         rx_pop,
    input  wire [15:0] rx_rdata,
    input  wire        rx_empty,
    input  wire        rx_full,

    // IRQ flags
    input  wire [7:0]  irq_flags,
    input  wire [7:0]  peer_irq_flags,
    output wire [7:0]  irq_clr,
    output wire [7:0]  peer_irq_clr,

    // SM state readback
    input  wire [4:0]  sm_pc,
    input  wire        sm_stalled,
    input  wire        sm_timeout_flag,
    input  wire [15:0] sm_x,
    input  wire [15:0] sm_y,
    input  wire [15:0] sm_isr,
    input  wire [15:0] sm_osr,
    input  wire [7:0]  sm_gpio_out,
    input  wire [7:0]  sm_gpio_oe
);

    localparam OP_IMEM_WR = 3'b001;
    localparam OP_REG_WR  = 3'b010;
    localparam OP_REG_RD  = 3'b011;
    localparam OP_TX_PUSH = 3'b100;
    localparam OP_RX_POP  = 3'b101;
    localparam OP_IRQ     = 3'b110;

    // ---------------------------------------------------------------
    // Pin synchronizers and SCLK edge detect
    // ---------------------------------------------------------------
    reg [2:0] sclk_q;      // [1:0] sync, [2] previous
    reg [1:0] mosi_q;
    reg [1:0] cs_n_q;

    wire sclk_rise = sclk_q[1] & ~sclk_q[2];
    wire sclk_fall = ~sclk_q[1] & sclk_q[2];
    wire cs_active = ~cs_n_q[1];
    wire mosi      = mosi_q[1];

    // ---------------------------------------------------------------
    // Frame shifter
    // ---------------------------------------------------------------
    reg [4:0]  bitcnt;     // bits received in the current frame, 0..23
    reg [15:0] sr_in;      // last 16 MOSI bits; holds the data word from
                           // bit 24 until the next SCLK rise (>= 6 clk)
    reg        irq_wr;     // write strobes fire the clk after bit 24,
                           // payload taken from sr_in / cmd
    reg [7:0]  cmd;        // latched at bit 8
    reg [15:0] miso_sr;    // response, MSB on MISO

    wire [7:0]  cmd_rx  = {sr_in[6:0], mosi};    // valid on rise with bitcnt == 7
    wire [15:0] data_rx = {sr_in[14:0], mosi};   // valid on rise with bitcnt == 23
    wire        cmd_end = cs_active & sclk_rise & (bitcnt == 5'd7);
    wire        frm_end = cs_active & sclk_rise & (bitcnt == 5'd23);

    // ---------------------------------------------------------------
    // Readback
    // ---------------------------------------------------------------
    wire [15:0] status = {3'd0, sm_pc, 2'd0, sm_timeout_flag, sm_stalled,
                          rx_full, rx_empty, tx_full, tx_empty};
    reg  [15:0] reg_rdata;

    always @(*) begin
        case (cmd_rx[4:0])
            5'd0:    reg_rdata = {14'd0, sm_reset, sm_en};
            5'd1:    reg_rdata = {3'd0, cfg_wrap_top, 3'd0, cfg_wrap_bottom};
            5'd2:    reg_rdata = {5'd0, cfg_ss_base, 3'd0, cfg_ss_pindirs, cfg_ss_opt, cfg_ss_count};
            5'd3:    reg_rdata = {1'b0, cfg_set_count, 1'b0, cfg_set_base, 1'b0, cfg_out_base,
                                  1'b0, cfg_in_base};
            5'd4:    reg_rdata = {10'd0, cfg_out_shr, cfg_in_shr, 1'b0, cfg_jmp_pin};
            5'd5:    reg_rdata = {cfg_clkdiv_frac, 3'd0, cfg_clkdiv_int};
            5'd6:    reg_rdata = {cfg_in_sel, 3'd0, cfg_od_mask};
            5'd8:    reg_rdata = status;
            5'd9:    reg_rdata = sm_x;
            5'd10:   reg_rdata = sm_y;
            5'd11:   reg_rdata = sm_isr;
            5'd12:   reg_rdata = sm_osr;
            5'd13:   reg_rdata = {sm_gpio_oe, sm_gpio_out};
            default: reg_rdata = 16'd0;
        endcase
    end

    reg [15:0] resp;

    always @(*) begin
        case (cmd_rx[7:5])
            OP_REG_RD: resp = reg_rdata;
            OP_RX_POP: resp = rx_empty ? 16'd0 : rx_rdata;
            OP_IRQ:    resp = {peer_irq_flags, irq_flags};
            default:   resp = status;
        endcase
    end

    // ---------------------------------------------------------------
    // Sequential
    // ---------------------------------------------------------------
    always @(posedge clk) begin
        if (!rst_n) begin
            sclk_q          <= 3'b000;
            mosi_q          <= 2'b00;
            cs_n_q          <= 2'b11;
            bitcnt          <= 5'd0;
            sr_in           <= 16'd0;
            irq_wr          <= 1'b0;
            cmd             <= 8'd0;
            miso_sr         <= 16'd0;
            spi_miso        <= 1'b0;
            imem_wr_en      <= 1'b0;
            tx_push         <= 1'b0;
            rx_pop          <= 1'b0;
            sm_en           <= 1'b0;
            sm_reset        <= 1'b0;
            cfg_wrap_bottom <= 5'd0;
            cfg_wrap_top    <= 5'd31;
            cfg_ss_count    <= 3'd0;
            cfg_ss_opt      <= 1'b0;
            cfg_ss_pindirs  <= 1'b0;
            cfg_ss_base     <= 3'd0;
            cfg_in_base     <= 3'd0;
            cfg_out_base    <= 3'd0;
            cfg_set_base    <= 3'd0;
            cfg_set_count   <= 3'd5;
            cfg_jmp_pin     <= 3'd0;
            cfg_in_shr      <= 1'b1;
            cfg_out_shr     <= 1'b1;
            cfg_clkdiv_int  <= 5'd1;
            cfg_clkdiv_frac <= 8'd0;
            cfg_od_mask     <= 8'd0;
            cfg_in_sel      <= 5'd0;
        end else begin
            sclk_q <= {sclk_q[1:0], spi_sclk};
            mosi_q <= {mosi_q[0], spi_mosi};
            cs_n_q <= {cs_n_q[0], spi_cs_n};

            // single-cycle strobes
            imem_wr_en <= 1'b0;
            tx_push    <= 1'b0;
            rx_pop     <= 1'b0;
            irq_wr     <= 1'b0;

            if (!cs_active) begin
                bitcnt <= 5'd0;
            end else if (sclk_rise) begin
                sr_in  <= data_rx;
                bitcnt <= (bitcnt == 5'd23) ? 5'd0 : bitcnt + 5'd1;
            end

            // bit 8: latch command, snapshot response, pop RX
            if (cmd_end) begin
                cmd     <= cmd_rx;
                miso_sr <= resp;
                rx_pop  <= (cmd_rx[7:5] == OP_RX_POP) & ~rx_empty;
            end else if (cs_active & sclk_fall & (bitcnt > 5'd8)) begin
                miso_sr <= {miso_sr[14:0], 1'b0};
            end

            spi_miso <= cs_active & (bitcnt >= 5'd8) & miso_sr[15];

            // bit 24: execute writes
            if (frm_end) begin
                case (cmd[7:5])
                    OP_IMEM_WR: imem_wr_en <= 1'b1;
                    OP_REG_WR: begin
                        case (cmd[4:0])
                            5'd0: begin
                                sm_en    <= data_rx[0];
                                sm_reset <= data_rx[1];
                            end
                            5'd1: begin
                                cfg_wrap_bottom <= data_rx[4:0];
                                cfg_wrap_top    <= data_rx[12:8];
                            end
                            5'd2: begin
                                cfg_ss_count   <= data_rx[2:0];
                                cfg_ss_opt     <= data_rx[3];
                                cfg_ss_pindirs <= data_rx[4];
                                cfg_ss_base    <= data_rx[10:8];
                            end
                            5'd3: begin
                                cfg_in_base   <= data_rx[2:0];
                                cfg_out_base  <= data_rx[6:4];
                                cfg_set_base  <= data_rx[10:8];
                                cfg_set_count <= data_rx[14:12];
                            end
                            5'd4: begin
                                cfg_jmp_pin <= data_rx[2:0];
                                cfg_in_shr  <= data_rx[4];
                                cfg_out_shr <= data_rx[5];
                            end
                            5'd5: begin
                                cfg_clkdiv_int  <= data_rx[4:0];
                                cfg_clkdiv_frac <= data_rx[15:8];
                            end
                            5'd6: begin
                                cfg_od_mask <= data_rx[7:0];
                                cfg_in_sel  <= data_rx[15:11];
                            end
                            default: ;
                        endcase
                    end
                    OP_TX_PUSH: tx_push <= 1'b1;
                    OP_IRQ:     irq_wr  <= 1'b1;
                    default: ;
                endcase
            end
        end
    end

    assign imem_wr_addr = cmd[4:0];
    assign imem_wr_data = sr_in;
    assign tx_wdata     = sr_in;
    assign irq_clr      = irq_wr ? sr_in[7:0]  : 8'd0;
    assign peer_irq_clr = irq_wr ? sr_in[15:8] : 8'd0;

endmodule

`default_nettype wire
