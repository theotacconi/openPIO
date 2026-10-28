// pau.v - Protocol Acceleration Unit of one state machine, ISA v2 (ISA.md, 2. The line layer).
//
// On a strobe (stb), as ISA.md Timing within a clock, step 2: the transmitter advances, then
// the receiver samples gpio_in and decodes; *_s is the state after that, what the instruction
// sees (status, queue lengths, RX bits). Step 3: the instruction's operation (one at most, all
// inputs below "instruction" are valid only with stb) acts on *_s and gives the next state.
//
// Queues hold their oldest bit at index 0. A TX entry is a bit and the PAU_CTL flags it was
// queued with (stuffing, monitor, CRC). The CRC and its registers are MSB-aligned (ISA.md, PAU):
// a step is then the same for every width, crc_a[31] is the bit that leaves, and a CRC append
// is crc_a bit-reversed. It takes at most one line bit (a TX bit as it starts, or an RX bit) and
// one CRC feed bit a strobe: two steps of a 32-bit LFSR.

`default_nettype none

module pau (
    input  wire        clk,
    input  wire        rst_n,        // synchronous, active-low (chip reset)
    input  wire        reset,        // CTRL.RESET, lands at the end of this clock
    input  wire        stb,          // a strobe of the SM

    // Host configuration (HOST.md)
    input  wire [1:0]  enc,          // PAU_MODE: line code (NRZ, NRZI, Manchester, BMC)
    input  wire        inv,          // invert the line
    input  wire        diff,         // RX: SE0 ends a frame (TX: the SM drives out_base + 1)
    input  wire [3:0]  stuff_len,    // 1..15, 0 = 16
    input  wire        sequal,       // stuffing after equal bits (else after 1s)
    input  wire [4:0]  baud_rst,     // PAU_BAUD after RESET
    input  wire [6:0]  crc_cfg,      // [4:0] width - 1, [5] init ones, [6] append complement
    input  wire [31:0] crc_poly,     // MSB-aligned; bits below the width are ignored
    input  wire [31:0] crc_res,
    input  wire [3:0]  in_base,
    input  wire [15:0] gpio_in,

    // Instruction (with stb)
    input  wire        ctl_we,       // SET PAU_CTL
    input  wire [4:0]  ctl_val,
    input  wire        pin_now,      // pin out_base's latch as the instruction sees it (TX on)
    input  wire        baud_we,      // SET / OUT PAU_BAUD
    input  wire [4:0]  baud_val,
    input  wire        tx_push,      // OUT PINS, TX on, room checked
    input  wire [4:0]  tx_n,         // 1..16
    input  wire [15:0] tx_bits,      // in time order (bit 0 first), zero from bit tx_n up
    input  wire        rx_take,      // IN PINS, RX on, rx_len_s >= rx_n checked
    input  wire [4:0]  rx_n,
    input  wire        crc_reset,
    input  wire        crc_append,   // the TX queue is empty (checked)
    input  wire        tx_abort,
    input  wire        feed,         // CRC feed: one ISR bit this strobe
    input  wire        feed_bit,

    output wire        tx_on,        // PAU_CTL [0]: OUT PINS feeds the TX queue
    output wire        rx_on,        // PAU_CTL [1]: IN PINS takes the RX queue
    output wire [10:0] status,       // PAU status 0..10 (ISA.md, Conditions), after step 2
    output wire [7:0]  status_r,     // status 0..7 before it: the host's (clock-start) view
    output wire [5:0]  tx_len_s,     // bits in the TX queue, after step 2
    output wire [5:0]  rx_len_s,
    output wire [15:0] rx_bits_s,    // the oldest 16 bits of the RX queue, after step 2
    output wire        drive,        // TX drives pin out_base (+1 with DIFF): from flip-flops
    output wire        line,         // its level (INV applied)
    output wire [31:0] crc           // CRC_LO / CRC_HI, MSB-aligned
);

    // ------------------------------------------------------------------ configuration
    reg  [4:0] ctl;                                      // PAU_CTL: [0] TX, [1] RX, [2] stuffing,
                                                         // [3] CRC, [4] monitor
    wire [4:0] slen = {stuff_len == 4'd0, stuff_len};    // 1..16
    reg  [4:0] baud;                                     // PAU_BAUD, H = baud (0 = 32)
    wire [4:0] h1   = baud - 5'd1;                       // H - 1: a half-bit is h1 + 1 strobes
    wire [5:0] h    = {baud == 5'd0, baud};              // H, 1..32

    wire       pin_in = gpio_in[in_base];
    wire       pin_in1 = gpio_in[in_base + 4'd1];
    wire       rx_in  = pin_in ^ inv;
    wire       se0    = ~pin_in & ~pin_in1;
    wire       se1    = pin_in & pin_in1;

    // CRC, MSB-aligned: its bits are the top width, 32 - width = ~crc_cfg[4:0] below them
    wire [4:0]  csh    = ~crc_cfg[4:0];
    wire [31:0] cmask  = 32'hFFFF_FFFF << csh;
    wire [31:0] cpoly  = crc_poly & cmask;
    wire [31:0] cres   = crc_res & cmask;
    wire [31:0] cinit  = crc_cfg[5] ? cmask : 32'd0;

    function [31:0] crc_step;
        input [31:0] c;
        input        b;
        crc_step = {c[30:0], 1'b0} ^ ({32{c[31] ^ b}} & cpoly);
    endfunction

    // ------------------------------------------------------------------ state
    reg [31:0] tq_b, tq_s, tq_m, tq_c;     // TX queue: bit, stuff, monitor, CRC flags
    reg [5:0]  tq_len;
    reg        t_act, t_half, t_bit, t_line, t_mon, t_err;
    reg [4:0]  t_cnt;                      // strobes left in the half-bit, minus 1
    reg [4:0]  ts_run;                     // TX stuffing: qualifying bits in a row
    reg        ts_last, ts_pend, ts_pval;  // last bit; a stuffed bit is owed, its value

    reg [31:0] rq_b;                       // RX queue
    reg [5:0]  rq_len;
    reg        r_prev, r_armed, r_mid, r_last;
    reg [5:0]  r_cnt;                      // NRZ / NRZI: strobes to the sample, minus 1
    reg [6:0]  r_since;                    // Manchester / BMC: strobes since the reference edge
    reg [7:0]  r_quiet;                    // strobes without an input edge (saturates)
    reg [4:0]  rs_run;                     // RX destuffing
    reg        rs_last, r_serr, r_ovr;

    reg [31:0] crc_a;

    // ------------------------------------------------------------------ step 2: TX
    wire drives = ~t_err & (ctl[0] | t_act | (tq_len != 6'd0) | ts_pend);

    reg [31:0] tq_b_s, tq_s_s, tq_m_s, tq_c_s;
    reg [5:0]  tq_len_s;
    reg        t_act_s, t_half_s, t_bit_s, t_line_s, t_mon_s, t_err_s;
    reg [4:0]  t_cnt_s, ts_run_s;
    reg        ts_last_s, ts_pend_s, ts_pval_s;
    reg        tx_feed;                    // a TX bit starting feeds the CRC
    reg        tb;                         // the bit starting

    always @(*) begin
        {tq_b_s, tq_s_s, tq_m_s, tq_c_s, tq_len_s} = {tq_b, tq_s, tq_m, tq_c, tq_len};
        {t_act_s, t_half_s, t_bit_s, t_line_s, t_mon_s, t_err_s} = {t_act, t_half, t_bit, t_line, t_mon, t_err};
        {t_cnt_s, ts_run_s, ts_last_s, ts_pend_s, ts_pval_s} = {t_cnt, ts_run, ts_last, ts_pend, ts_pval};
        tx_feed = 1'b0;
        tb      = ts_pval;
        if (stb & drives) begin
            if (t_act & (t_cnt != 5'd0)) begin
                t_cnt_s = t_cnt - 5'd1;
            end else if (t_act & ~t_half) begin               // middle of the bit
                if (t_mon & (rx_in != t_line)) begin          // monitor: quiet until TX on
                    t_err_s   = 1'b1;
                    tq_len_s  = 6'd0;
                    {tq_b_s, tq_s_s, tq_m_s, tq_c_s} = 128'd0;
                    t_act_s   = 1'b0;
                    ts_pend_s = 1'b0;
                end else begin
                    t_half_s = 1'b1;
                    t_cnt_s  = h1;
                    if (enc == 2'd2)
                        t_line_s = t_bit;                     // Manchester: b in the 2nd half
                    else if (enc == 2'd3 & t_bit)
                        t_line_s = ~t_line;                   // BMC: a 1 toggles mid-bit
                end
            end else begin                                    // idle, or the end of the bit
                t_act_s = 1'b0;
                if (ts_pend) begin                            // the stuffed bit first
                    tb        = ts_pval;
                    ts_pend_s = 1'b0;
                    ts_run_s  = {4'd0, sequal};               // ends a run of 1s, starts one of equal bits
                    ts_last_s = ts_pval;
                end else if (tq_len != 6'd0) begin
                    tb       = tq_b[0];
                    t_mon_s  = tq_m[0];
                    tx_feed  = tq_c[0];
                    tq_b_s   = {1'b0, tq_b[31:1]};
                    tq_s_s   = {1'b0, tq_s[31:1]};
                    tq_m_s   = {1'b0, tq_m[31:1]};
                    tq_c_s   = {1'b0, tq_c[31:1]};
                    tq_len_s = tq_len - 6'd1;
                    if (tq_s[0]) begin
                        ts_run_s  = (sequal ? (tq_b[0] == ts_last) : tq_b[0]) ? ts_run + 5'd1
                                                                               : {4'd0, sequal};
                        ts_last_s = tq_b[0];
                        ts_pend_s = ts_run_s >= slen;
                        ts_pval_s = sequal & ~tq_b[0];
                    end else begin
                        ts_run_s = 5'd0;                      // an unstuffed bit restarts the count
                    end
                end
                if (ts_pend | (tq_len != 6'd0)) begin
                    t_act_s  = 1'b1;
                    t_half_s = 1'b0;
                    t_cnt_s  = h1;
                    t_bit_s  = tb;
                    case (enc)
                        2'd0:    t_line_s = tb;               // NRZ
                        2'd1:    t_line_s = t_line ^ ~tb;     // NRZI: a 0 toggles
                        2'd2:    t_line_s = ~tb;              // Manchester: ~b in the 1st half
                        default: t_line_s = ~t_line;          // BMC: toggles at every bit
                    endcase
                end
            end
        end
    end

    wire drives_s = ~t_err_s & (ctl[0] | t_act_s | (tq_len_s != 6'd0) | ts_pend_s);

    // ------------------------------------------------------------------ step 2: RX
    reg [31:0] rq_b_s;
    reg [5:0]  rq_len_s, r_cnt_s;
    reg        r_prev_s, r_armed_s, r_mid_s, r_last_s;
    reg [6:0]  r_since_s;
    reg [7:0]  r_quiet_s;
    reg [4:0]  rs_run_s;
    reg        rs_last_s, r_serr_s, r_ovr_s;
    reg        rb, rb_v;                   // a decoded bit
    reg        rx_feed;                    // it feeds the CRC

    wire       edge_in = rx_in != r_prev;
    wire [6:0] s1      = r_since + 7'd1;
    wire [7:0] t2      = {s1, 1'b0};       // in half-H units: compare with H, 3H, 5H
    wire [7:0] h_1     = {2'b0, h};
    wire [7:0] h_3     = h_1 + {1'b0, h, 1'b0};
    wire [7:0] h_5     = h_1 + {h, 2'b0};
    wire       w_mid   = (t2 >= h_1) & (t2 < h_3);
    wire       w_bound = (t2 >= h_3) & (t2 <= h_5);
    wire       w_late  = t2 > h_5;

    always @(*) begin
        {rq_b_s, rq_len_s, r_cnt_s, r_since_s, r_quiet_s} = {rq_b, rq_len, r_cnt, r_since, r_quiet};
        {r_prev_s, r_armed_s, r_mid_s, r_last_s} = {r_prev, r_armed, r_mid, r_last};
        {rs_run_s, rs_last_s, r_serr_s, r_ovr_s} = {rs_run, rs_last, r_serr, r_ovr};
        rb = rx_in;
        rb_v = 1'b0;
        if (stb & ctl[1]) begin
            r_prev_s = rx_in;
            if (diff & se0) begin                             // DIFF: SE0 ends the frame
                r_armed_s = 1'b1;
                r_quiet_s = 8'd0;
            end else begin
                r_quiet_s = edge_in ? 8'd0 : r_quiet + {7'd0, ~&r_quiet};
                if (~enc[1]) begin                            // NRZ / NRZI: sample mid-bit
                    if (edge_in) begin
                        r_armed_s = 1'b0;
                        r_cnt_s   = {1'b0, h1};
                    end else if (~r_armed) begin
                        if (r_cnt != 6'd0) begin
                            r_cnt_s = r_cnt - 6'd1;
                        end else begin
                            r_cnt_s  = {h1, 1'b1};            // 2H - 1
                            rb_v     = 1'b1;
                            rb       = enc[0] ? rx_in == r_last : rx_in;
                            r_last_s = rx_in;
                        end
                    end
                end else if (r_armed) begin                   // Manchester / BMC: edge timing
                    if (edge_in) begin
                        r_armed_s = 1'b0;
                        r_since_s = 7'd0;
                        r_mid_s   = 1'b0;
                        rb_v      = ~enc[0];                  // Manchester: the first edge is mid-bit
                    end
                end else begin
                    r_since_s = s1;
                    if (edge_in) begin
                        if (~enc[0]) begin                    // Manchester
                            if (w_bound) begin
                                rb_v = 1'b1;
                                r_since_s = 7'd0;
                            end
                        end else if (w_mid) begin             // BMC
                            r_mid_s = 1'b1;
                        end else if (w_bound) begin
                            rb_v = 1'b1;
                            rb   = r_mid;
                            r_since_s = 7'd0;
                            r_mid_s   = 1'b0;
                        end
                    end else if (w_late) begin                // no edge in time: end of frame
                        rb_v = enc[0];                        // BMC: the last bit has no boundary
                        rb   = r_mid;
                        r_armed_s = 1'b1;
                    end
                end
            end
        end
        // destuffing, then the CRC and the queue
        rx_feed = 1'b0;
        if (rb_v) begin
            if (ctl[2]) begin
                if (rs_run < slen) begin
                    rs_run_s  = (sequal ? (rb == rs_last) : rb) ? rs_run + 5'd1 : {4'd0, sequal};
                    rs_last_s = rb;
                end else begin                                // the stuffed bit's slot
                    rs_run_s  = {4'd0, sequal};
                    rs_last_s = rb;
                    if (rb == (sequal & ~rs_last))
                        rb_v = 1'b0;                          // dropped
                    else
                        r_serr_s = 1'b1;                      // a wrong one is kept
                end
            end
        end
        if (rb_v) begin
            rx_feed = ctl[3] & ~drives_s;
            if (rq_len == 6'd32)
                r_ovr_s = 1'b1;
            else begin
                rq_b_s   = rq_b | ({31'd0, rb} << rq_len[4:0]);
                rq_len_s = rq_len + 6'd1;
            end
        end
    end

    // ------------------------------------------------------------------ step 2: CRC
    wire [31:0] crc_s = (tx_feed | rx_feed) ? crc_step(crc_a, tx_feed ? tb : rb) : crc_a;

    // ------------------------------------------------------------------ status, outputs
    assign status = {t_err_s, se1, se0, r_ovr_s, r_serr_s, crc_s == 32'd0, crc_s == cres,
                     rq_len_s != 6'd0, tq_len_s <= 6'd16,
                     ~ctl[1] | (r_quiet_s >= {h, 2'b00}),
                     ~t_act_s & (tq_len_s == 6'd0) & ~ts_pend_s};
    assign status_r = {r_ovr, r_serr, crc_a == 32'd0, crc_a == cres,
                       rq_len != 6'd0, tq_len <= 6'd16,
                       ~ctl[1] | (r_quiet >= {h, 2'b00}),
                       ~t_act & (tq_len == 6'd0) & ~ts_pend};
    assign tx_on     = ctl[0];
    assign rx_on     = ctl[1];
    assign tx_len_s  = tq_len_s;
    assign rx_len_s  = rq_len_s;
    assign rx_bits_s = rq_b_s[15:0];
    assign drive     = drives;
    assign line      = t_line ^ inv;
    assign crc       = crc_a;

    // ------------------------------------------------------------------ step 3 and registers
    wire [31:0] tx_mask  = ~(32'hFFFF_FFFF << tx_n) << tq_len_s[4:0];
    wire [31:0] app_bits;                                     // CRC append: MSB first
    wire [31:0] app_word = crc_s ^ (crc_cfg[6] ? cmask : 32'd0);  // the last data bit fed in step 2
    genvar i;
    generate
        for (i = 0; i < 32; i = i + 1) begin : g_app
            assign app_bits[i] = app_word[31 - i];
        end
    endgenerate
    wire [31:0] app_mask = 32'hFFFF_FFFF >> csh;              // the low width bits
    wire        tx_rise  = ctl_we & ctl_val[0] & ~ctl[0];
    wire        rx_rise  = ctl_we & ctl_val[1] & ~ctl[1];

    always @(posedge clk) begin
        if (!rst_n || reset) begin
            ctl     <= 5'd0;
            baud    <= rst_n ? baud_rst : 5'd0;
            crc_a   <= rst_n ? cinit : 32'd0;
            {tq_b, tq_s, tq_m, tq_c} <= 128'd0;
            tq_len  <= 6'd0;
            {t_act, t_half, t_bit, t_line, t_mon, t_err} <= 6'd0;
            t_cnt   <= 5'd0;
            {ts_run, ts_last, ts_pend, ts_pval} <= 8'd0;
            rq_b    <= 32'd0;
            rq_len  <= 6'd0;
            {r_prev, r_mid, r_last} <= 3'd0;
            r_armed <= 1'b1;
            r_cnt   <= 6'd0;
            r_since <= 7'd0;
            r_quiet <= 8'd0;
            {rs_run, rs_last, r_serr, r_ovr} <= 8'd0;
        end else begin
            {tq_b, tq_s, tq_m, tq_c, tq_len} <= {tq_b_s, tq_s_s, tq_m_s, tq_c_s, tq_len_s};
            {t_act, t_half, t_bit, t_line, t_mon, t_err} <= {t_act_s, t_half_s, t_bit_s, t_line_s, t_mon_s, t_err_s};
            {t_cnt, ts_run, ts_last, ts_pend, ts_pval} <= {t_cnt_s, ts_run_s, ts_last_s, ts_pend_s, ts_pval_s};
            {rq_b, rq_len, r_cnt, r_since, r_quiet} <= {rq_b_s, rq_len_s, r_cnt_s, r_since_s, r_quiet_s};
            {r_prev, r_armed, r_mid, r_last} <= {r_prev_s, r_armed_s, r_mid_s, r_last_s};
            {rs_run, rs_last, r_serr, r_ovr} <= {rs_run_s, rs_last_s, r_serr_s, r_ovr_s};
            crc_a <= feed ? crc_step(crc_s, feed_bit) : crc_s;

            if (stb) begin
                if (ctl_we) begin
                    ctl <= ctl_val;
                    if (tx_rise) begin
                        t_err <= 1'b0;
                        if (~drives_s) begin                  // the encoder starts from the pin
                            t_line  <= pin_now ^ inv;
                            ts_run  <= 5'd0;
                            ts_pend <= 1'b0;
                        end
                    end
                    if (rx_rise) begin                        // a new reception
                        rq_len  <= 6'd0;
                        rq_b    <= 32'd0;
                        r_armed <= 1'b1;
                        r_mid   <= 1'b0;
                        r_cnt   <= 6'd0;
                        r_since <= 7'd0;
                        r_quiet <= 8'd0;
                        r_prev  <= rx_in;
                        r_last  <= rx_in;
                        {r_serr, r_ovr} <= 2'b00;
                    end
                    if (rx_rise | ~ctl_val[2])
                        {rs_run, rs_last} <= 6'd0;
                end
                if (baud_we)
                    baud <= baud_val;
                if (tx_push & ~t_err_s) begin
                    tq_b   <= tq_b_s | ({16'd0, tx_bits} << tq_len_s[4:0]);
                    tq_s   <= tq_s_s | (tx_mask & {32{ctl[2]}});
                    tq_m   <= tq_m_s | (tx_mask & {32{ctl[4]}});
                    tq_c   <= tq_c_s | (tx_mask & {32{ctl[3]}});
                    tq_len <= tq_len_s + {1'b0, tx_n};
                end
                if (rx_take) begin
                    rq_b   <= rq_b_s >> rx_n;
                    rq_len <= rq_len_s - {1'b0, rx_n};
                end
                if (crc_reset) begin
                    crc_a <= cinit;
                    {r_serr, r_ovr} <= 2'b00;
                end
                if (crc_append & ~t_err_s) begin
                    tq_b   <= app_bits;
                    tq_s   <= app_mask & {32{ctl[2]}};
                    tq_m   <= app_mask & {32{ctl[4]}};
                    tq_c   <= 32'd0;
                    tq_len <= {1'b0, crc_cfg[4:0]} + 6'd1;
                end
                if (tx_abort) begin
                    ctl[0]  <= 1'b0;
                    {tq_b, tq_s, tq_m, tq_c} <= 128'd0;
                    tq_len  <= 6'd0;
                    t_act   <= 1'b0;
                    ts_pend <= 1'b0;
                end
            end
        end
    end

endmodule

`default_nettype wire
