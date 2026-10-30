// openpio.v - the openPIO chip without its pads and memories (sim/model/chip.py): N state
// machines with their FIFOs, the shared pin latches, the chip registers and the host access.
//
// A host access (HOST.md, ISA.md Timing within a clock) takes one clock beside the SMs: its
// reads take the clock-start state, its writes land at the end, over the SMs' and after the
// FIFO moves. IMEM_RD reads the SRAM, so its word comes a clock later (h_imem_rdata); the IMEM
// cannot change in between while the SM is stopped, and reads 0 while it runs.
//
// The IMEMs and FIFO SRAMs are outside, on flat buses (SM i at bits [16 i +: 16] etc.): one
// port each, a read's word a clock later.

`default_nettype none

module openpio #(
    parameter N_SM       = 2,              // 2 or 4: peers are SM i and i ^ 1
    parameter PADS_OUT   = 16'hFCFF,       // pins with an output pad (HOST.md, Pins)
    parameter PADS_IN    = 16'hF8FF,       // pins with an input pad
    parameter PADS_BIDIR = 16'h00FF        // pins whose output and input are one line
) (
    input  wire               clk,
    input  wire               rst_n,

    // host access (chip.py host()): one per clock
    input  wire               h_valid,
    input  wire [2:0]         h_op,          // HOST.md SPI commands
    input  wire [1:0]         h_sm,          // SMs beyond N_SM - 1 mean the last one
    input  wire [5:0]         h_addr,        // a register (0x00..0x3F), or an IMEM word
    input  wire [15:0]        h_wdata,
    output reg  [15:0]        h_rdata,       // REG_RD, RX_POP: the clock-start state
    output wire [15:0]        h_imem_rdata,  // IMEM_RD: in the clock after the access
    output wire [15:0]        h_status,      // STATUS of SM h_sm (MISO between reads)
    output wire               host_irq,

    // pins: the input pads, and the output pads with their enables
    input  wire [15:0]        pin_in,
    output wire [15:0]        pin_out,
    output wire [15:0]        pin_oe,

    // IMEMs (64 x 16) and FIFO SRAMs (256 x 16), one port each
    output reg  [6*N_SM-1:0]  imem_addr,
    output reg  [N_SM-1:0]    imem_we,
    output wire [15:0]        imem_wdata,
    input  wire [16*N_SM-1:0] imem_rdata,
    output wire [8*N_SM-1:0]  ram_addr,
    output wire [N_SM-1:0]    ram_we,
    output wire [N_SM-1:0]    ram_re,
    output wire [16*N_SM-1:0] ram_wdata,
    input  wire [16*N_SM-1:0] ram_rdata
);

    localparam OP_REG_WR = 3'd2, OP_REG_RD = 3'd3, OP_TX_PUSH = 3'd4, OP_RX_POP = 3'd5,
               OP_IMEM_WR = 3'd6, OP_IMEM_RD = 3'd7;
    localparam A_CTRL = 6'h20, A_GPIO = 6'h21, A_PINS = 6'h22, A_PINS_OE = 6'h23,
               A_PINS_IN = 6'h24, A_IRQ_SUM = 6'h25, A_ID = 6'h26, A_PADS_OUT = 6'h27,
               A_PADS_IN = 6'h28, A_PADS_BIDIR = 6'h29;

    localparam [3:0] N4 = N_SM[3:0];
    wire [1:0]  hsm = N_SM == 2 ? {1'b0, |h_sm} : h_sm;    // chip.py spi(): at most the last SM

    // ------------------------------------------------------------------ chip state
    reg  [15:0] sync1, gpio_in;            // 2-flip-flop input synchronizer
    reg  [15:0] latch_out, latch_dir, od_mask;
    reg  [3:0]  en, stepping;

    wire        h_chip = h_addr[5:4] == 2'b10;              // 0x20..0x2F
    wire        ctrl_wr = h_valid & h_op == OP_REG_WR & h_addr == A_CTRL;
    wire [3:0]  sm_mask = (4'd1 << N_SM) - 4'd1;
    wire [3:0]  en_new  = h_wdata[3:0] & sm_mask;
    wire [N_SM-1:0] rst_req = ctrl_wr ? h_wdata[4 +: N_SM] : {N_SM{1'b0}};

    // ------------------------------------------------------------------ the SMs and FIFOs
    wire [16*N_SM-1:0] sm_rdata, out_we, out_val, dir_we, dir_val, pau_we, pau_val;
    wire [16*N_SM-1:0] tx_data, rx_data, fifo_rdata;
    wire [8*N_SM-1:0]  irq, tx_level, rx_level;
    wire [6*N_SM-1:0]  pc, sm_imem;
    wire [10*N_SM-1:0] irq_mask;
    wire [3*N_SM-1:0]  peer_idx;
    wire [N_SM-1:0]    strobe, stalled, peer_we, peer_val, tx_empty, tx_pop, rx_full, rx_push,
                       h_room;
    wire [N_SM-1:0]    irq_req;
    wire [3:0]         strobe4  = {{(4-N_SM){1'b0}}, strobe};
    wire [3:0]         stalled4 = {{(4-N_SM){1'b0}}, stalled};
    wire [3:0]         room4    = {{(4-N_SM){1'b0}}, h_room};

    genvar i;
    generate
        for (i = 0; i < N_SM; i = i + 1) begin : g_sm
            wire sel = h_valid & hsm == i;
            wire tx_fifo_empty = tx_level[8*i +: 8] == 8'd0;
            wire rx_fifo_full  = rx_level[8*i +: 8] >= 8'd133;

            openpio_sm u_sm (
                .clk           (clk),
                .rst_n         (rst_n),
                .en            (en[i]),
                .run           (en[i] | stepping[i]),
                .reset         (rst_req[i]),
                .strobe        (strobe[i]),
                .imem_addr     (sm_imem[6*i +: 6]),
                .instr         (imem_rdata[16*i +: 16]),
                .h_we          (sel & h_op == OP_REG_WR & ~h_chip),
                .h_addr        (h_addr),
                .h_wdata       (h_wdata),
                .h_rdata       (sm_rdata[16*i +: 16]),
                .gpio_in       (gpio_in),
                .latch_out     (latch_out),
                .latch_dir     (latch_dir),
                .out_we        (out_we[16*i +: 16]),
                .out_val       (out_val[16*i +: 16]),
                .dir_we        (dir_we[16*i +: 16]),
                .dir_val       (dir_val[16*i +: 16]),
                .pau_we        (pau_we[16*i +: 16]),
                .pau_val       (pau_val[16*i +: 16]),
                .tx_empty      (tx_empty[i]),
                .tx_data       (tx_data[16*i +: 16]),
                .tx_pop        (tx_pop[i]),
                .rx_full       (rx_full[i]),
                .rx_push       (rx_push[i]),
                .rx_data       (rx_data[16*i +: 16]),
                .tx_fifo_empty (tx_fifo_empty),
                .rx_fifo_full  (rx_fifo_full),
                .irq           (irq[8*i +: 8]),
                .peer_irq      (irq[8*(i ^ 1) +: 8]),
                .peer_we       (peer_we[i]),
                .peer_idx      (peer_idx[3*i +: 3]),
                .peer_val      (peer_val[i]),
                .in_we         (peer_we[i ^ 1]),
                .in_idx        (peer_idx[3*(i ^ 1) +: 3]),
                .in_val        (peer_val[i ^ 1]),
                .irq_mask      (irq_mask[10*i +: 10]),
                .pc            (pc[6*i +: 6]),
                .stalled       (stalled[i])
            );

            fifo u_fifo (
                .clk       (clk),
                .rst_n     (rst_n),
                .reset     (rst_req[i]),
                .tx_empty  (tx_empty[i]),
                .tx_data   (tx_data[16*i +: 16]),
                .tx_pop    (tx_pop[i]),
                .rx_full   (rx_full[i]),
                .rx_push   (rx_push[i]),
                .rx_data   (rx_data[16*i +: 16]),
                .tx_level  (tx_level[8*i +: 8]),
                .rx_level  (rx_level[8*i +: 8]),
                .h_push    (sel & h_op == OP_TX_PUSH),
                .h_wdata   (h_wdata),
                .h_room    (h_room[i]),
                .h_pop     (sel & h_op == OP_RX_POP),
                .h_rdata   (fifo_rdata[16*i +: 16]),
                .ram_addr  (ram_addr[8*i +: 8]),
                .ram_we    (ram_we[i]),
                .ram_re    (ram_re[i]),
                .ram_wdata (ram_wdata[16*i +: 16]),
                .ram_rdata (ram_rdata[16*i +: 16])
            );

            // HOST_IRQ: a flag in IRQ_MASK [7:0], RX FIFO not empty [8], TX FIFO <= 64 words [9]
            assign irq_req[i] = |(irq[8*i +: 8] & irq_mask[10*i +: 8])
                                | (irq_mask[10*i + 8] & rx_level[8*i +: 8] != 8'd0)
                                | (irq_mask[10*i + 9] & tx_level[8*i +: 8] <= 8'd64);
        end
    endgenerate

    // ------------------------------------------------------------------ pins
    // latches: the writes of the clock in SM order (the highest SM wins); values: the latch with
    // each driving PAU over it in SM order, then the pads (open drain on bidirectional ones)
    reg [15:0] lo_d, ld_d, pins;
    integer k;
    always @(*) begin
        lo_d = latch_out;
        ld_d = latch_dir;
        pins = latch_out;
        for (k = 0; k < N_SM; k = k + 1) begin
            lo_d = (lo_d & ~out_we[16*k +: 16]) | (out_val[16*k +: 16] & out_we[16*k +: 16]);
            ld_d = (ld_d & ~dir_we[16*k +: 16]) | (dir_val[16*k +: 16] & dir_we[16*k +: 16]);
            pins = (pins & ~pau_we[16*k +: 16]) | (pau_val[16*k +: 16] & pau_we[16*k +: 16]);
        end
    end
    localparam [15:0] PADS_ONLY = PADS_OUT & ~PADS_BIDIR;
    assign pin_out = (pins & ~od_mask & PADS_BIDIR) | (pins & PADS_ONLY);
    assign pin_oe  = (latch_dir & ~(pins & od_mask) & PADS_BIDIR) | PADS_ONLY;

    // ------------------------------------------------------------------ host reads
    wire [5:0]  s_pc     = pc[6*hsm +: 6];
    wire [7:0]  s_txl    = tx_level[8*hsm +: 8];
    wire [7:0]  s_rxl    = rx_level[8*hsm +: 8];
    assign h_status = {2'd0, s_pc, 2'd0, en[hsm], stalled4[hsm], s_rxl >= 8'd133,
                       s_rxl == 8'd0, ~room4[hsm], s_txl == 8'd0};

    always @(*) begin
        h_rdata = 16'd0;
        if (h_op == OP_RX_POP)
            h_rdata = fifo_rdata[16*hsm +: 16];
        else if (h_op == OP_REG_RD) begin
            case (h_addr)
                6'h00:        h_rdata = h_status;
                6'h1C:        h_rdata = {s_rxl, s_txl};
                A_CTRL:       h_rdata = {12'd0, en};
                A_GPIO:       h_rdata = od_mask;
                A_PINS:       h_rdata = pin_out;
                A_PINS_OE:    h_rdata = pin_oe;
                A_PINS_IN:    h_rdata = gpio_in;
                A_IRQ_SUM:    h_rdata = {{(16-N_SM){1'b0}}, irq_req};
                A_ID:         h_rdata = {4'd3, N4, 8'h50};
                A_PADS_OUT:   h_rdata = PADS_OUT;
                A_PADS_IN:    h_rdata = PADS_IN;
                A_PADS_BIDIR: h_rdata = PADS_BIDIR;
                default:      h_rdata = h_chip ? 16'd0 : sm_rdata[16*hsm +: 16];
            endcase
        end
    end
    assign host_irq = |irq_req;

    // ------------------------------------------------------------------ IMEM ports
    // the SM fetches; the host writes and reads while it is stopped
    wire im_host = h_valid & (h_op == OP_IMEM_WR | h_op == OP_IMEM_RD) & ~en[hsm];
    reg  im_rd_v;                          // the last clock was an IMEM_RD of a stopped SM
    reg  [1:0] im_rd_sm;
    integer m;
    always @(*) begin
        for (m = 0; m < N_SM; m = m + 1) begin
            imem_addr[6*m +: 6] = (im_host & hsm == m[1:0]) ? h_addr : sm_imem[6*m +: 6];
            imem_we[m] = im_host & hsm == m[1:0] & h_op == OP_IMEM_WR;
        end
    end
    assign imem_wdata   = h_wdata;
    assign h_imem_rdata = im_rd_v ? imem_rdata[16*im_rd_sm +: 16] : 16'd0;

    // ------------------------------------------------------------------ registers
    always @(posedge clk) begin
        if (!rst_n) begin
            {sync1, gpio_in} <= 32'd0;
            {latch_out, latch_dir, od_mask} <= 48'd0;
            {en, stepping} <= 8'd0;
            {im_rd_v, im_rd_sm} <= 3'd0;
        end else begin
            sync1   <= pin_in & PADS_IN;
            gpio_in <= sync1;
            latch_out <= lo_d;
            latch_dir <= ld_d;
            stepping  <= stepping & ~strobe4;
            im_rd_v   <= im_host & h_op == OP_IMEM_RD;
            im_rd_sm  <= hsm;
            if (h_valid & h_op == OP_REG_WR) begin
                if (h_addr == A_CTRL) begin
                    en <= en_new;
                    stepping <= (stepping & ~strobe4) | (h_wdata[11:8] & sm_mask & ~en_new);
                end
                if (h_addr == A_GPIO)
                    od_mask <= h_wdata;
            end
        end
    end

endmodule

`default_nettype wire
