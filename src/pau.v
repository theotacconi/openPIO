// pau.v - Protocol Acceleration Unit of one state machine, ISA v3 (ISA.md, 2. The line layer).
//
// On a strobe (stb), as ISA.md Timing within a clock, step 2: the transmitter advances, then
// the receiver samples gpio_in and decodes; *_s is the state after that, what the instruction
// sees (status, queue lengths, RX bits). Step 3: the instruction's operation (one at most, all
// inputs below "instruction" are valid only with stb) acts on *_s and gives the next state.
//
// Queues hold one word, their oldest bit at index 0. A TX entry is a bit and the PAU_CTL flags
// it was queued with (stuffing, monitor, CRC). The CRC and its registers are MSB-aligned
// (ISA.md, PAU): a step is then the same for every width, and crc_a[31] is the bit that leaves.
// A CRC append sends crc_a[31] and feeds it back, which only shifts crc_a: the CRC goes out of
// its own register, not through the queue. The CRC takes at most one line bit (a TX bit as it
// starts, or an RX bit) and one CRC feed bit a strobe: two steps of a 32-bit LFSR.
//
// The line code is PAU_CODE: the action (hold, toggle, 0, 1) at the start of each half-bit for
// the bit and the one before it. Level codes (every second half holds) are sampled mid-bit;
// transition codes are taken half-bit by half-bit from the edge timing. With EXT the bits come
// on the edges of pin in_base + 1, the external clock.

`default_nettype none

module pau (
    input  wire        clk,
    input  wire        rst_n,        // synchronous, active-low (chip reset)
    input  wire        reset,        // CTRL.RESET, lands at the end of this clock
    input  wire        stb,          // a strobe of the SM

    // Host configuration (HOST.md)
    input  wire [15:0] code,         // PAU_CODE: the line code table
    input  wire        inv,          // PAU_MODE: invert the line
    input  wire        align,        // RX, transition codes: a frame's first edge is mid-bit
    input  wire        ext,          // bit timing from pin in_base + 1
    input  wire        cpol,         // the bit clock's idle level
    input  wire        cpha,         // and phase
    input  wire [3:0]  stuff_len,    // 1..15, 0 = 16
    input  wire        sequal,       // stuffing after equal bits (else after 1s)
    input  wire [4:0]  baud_rst,     // PAU_CFG: PAU_BAUD after RESET
    input  wire [1:0]  run,          // a transition code's longest run of half-bits, minus 2
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
    input  wire        crc_append,   // the TX queue is empty, no append under way (checked)
    input  wire        tx_abort,
    input  wire        feed,         // CRC feed: one ISR bit this strobe
    input  wire        feed_bit,

    output wire        tx_on,        // PAU_CTL [0]: OUT PINS feeds the TX queue
    output wire        rx_on,        // PAU_CTL [1]: IN PINS takes the RX queue
    output wire [12:0] status,       // PAU status 0..12 (ISA.md, Conditions), after step 2
    output wire [7:0]  status_r,     // status 0..7 before it: the host's (clock-start) view
    output wire [4:0]  tx_len_s,     // bits in the TX queue, after step 2
    output wire [4:0]  rx_len_s,
    output wire        crc_busy,     // a CRC append under way, after step 2: the CRC is sent
    output wire [15:0] rx_bits_s,    // the oldest 16 bits of the RX queue, after step 2
    output wire        drive,        // TX drives pin out_base (and out_base + 1, PIN2): from flip-flops
    output wire        line,         // its level (INV applied)
    output wire        bit_clk,      // the bit clock for PIN2: CPOL, or the half-bit's level
    output wire [31:0] crc           // CRC_LO / CRC_HI, MSB-aligned
);

    // ------------------------------------------------------------------ configuration
    reg  [4:0] ctl;                                      // PAU_CTL: [0] TX, [1] RX, [2] stuffing,
                                                         // [3] CRC, [4] monitor
    wire [4:0] slen = {stuff_len == 4'd0, stuff_len};    // 1..16
    reg  [4:0] baud;                                     // PAU_BAUD, H = baud (0 = 32)
    wire [4:0] h1   = baud - 5'd1;                       // H - 1: a half-bit is h1 + 1 strobes
    wire [5:0] h    = {baud == 5'd0, baud};              // H, 1..32
    wire       level_code = code[15:8] == 8'd0;          // every second half holds

    wire       pin_in = gpio_in[in_base];
    wire       pin_in1 = gpio_in[in_base + 4'd1];
    wire       rx_in  = pin_in ^ inv;

    // the action of half `hf` for bit b after bit p, applied to the line level `lv`
    function act;
        input [15:0] tbl;
        input        hf, b, p, lv;
        reg   [1:0]  a;
        begin
            a = tbl[{hf, b, p, 1'b0} +: 2];
            act = a[1] ? a[0] : lv ^ a[0];
        end
    endfunction

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
    reg [15:0] tq_b, tq_s, tq_m, tq_c;     // TX queue: bit, stuff, monitor, CRC flags
    reg [4:0]  tq_len;
    reg [5:0]  a_cnt;                      // CRC append: bits still to send, before the queue
    reg        a_s, a_m;                   // their stuffing and monitor flags
    reg        t_act, t_half, t_bit, t_line, t_mon, t_err;
    reg        t_p, t_last;                // the bit before the one sent; the last bit started
    reg [4:0]  t_cnt;                      // strobes left in the half-bit, minus 1
    reg [4:0]  ts_run;                     // TX stuffing: qualifying bits in a row
    reg        ts_last, ts_pend, ts_pval;  // last bit; a stuffed bit is owed, its value
    reg        e_prev;                     // the external clock pin at the last strobe

    reg [15:0] rq_b;                       // RX queue
    reg [4:0]  rq_len;
    reg        r_prev, r_armed, r_last, r_p;
    reg [5:0]  r_cnt;                      // level codes: strobes to the sample, minus 1
    reg [7:0]  r_since;                    // transition codes: strobes since the last edge
    reg [8:0]  r_thr;                      // the next half-bit without an edge: 2 x since >= thr
    reg [2:0]  r_run;                      // half-bits at one level since the last edge
    reg        r_half, r_h0, r_end;        // the half under way; the bit's first half; its end
    reg [7:0]  r_quiet;                    // strobes without an input edge (saturates)
    reg [4:0]  rs_run;                     // RX destuffing
    reg        rs_last, r_serr, r_ovr;

    reg [31:0] crc_a;

    // ------------------------------------------------------------------ step 2: clock edges
    wire       e_edge   = pin_in1 != e_prev;
    wire       e_sample = e_edge & (pin_in1 == (cpol == cpha));
    wire       e_shift  = e_edge & (pin_in1 != (cpol == cpha));

    // ------------------------------------------------------------------ step 2: TX
    wire pending = (tq_len != 5'd0) | ts_pend | (a_cnt != 6'd0);
    wire drives  = ~t_err & (ctl[0] | t_act | pending);

    reg [15:0] tq_b_s, tq_s_s, tq_m_s, tq_c_s;
    reg [4:0]  tq_len_s;
    reg [5:0]  a_cnt_s;
    reg        t_act_s, t_half_s, t_bit_s, t_line_s, t_mon_s, t_err_s, t_p_s, t_last_s;
    reg [4:0]  t_cnt_s, ts_run_s;
    reg        ts_last_s, ts_pend_s, ts_pval_s;
    reg        tx_feed;                    // a TX bit starting feeds the CRC
    reg        a_feed;                     // an appended bit starting shifts the CRC
    reg        tb, ts;                     // the bit starting, its stuffing flag
    reg        t_start, t_end;             // a bit starts, the bit under way ends

    always @(*) begin
        {tq_b_s, tq_s_s, tq_m_s, tq_c_s, tq_len_s, a_cnt_s} = {tq_b, tq_s, tq_m, tq_c, tq_len, a_cnt};
        {t_act_s, t_half_s, t_bit_s, t_line_s, t_mon_s, t_err_s} = {t_act, t_half, t_bit, t_line, t_mon, t_err};
        {t_p_s, t_last_s} = {t_p, t_last};
        {t_cnt_s, ts_run_s, ts_last_s, ts_pend_s, ts_pval_s} = {t_cnt, ts_run, ts_last, ts_pend, ts_pval};
        tx_feed = 1'b0;
        a_feed  = 1'b0;
        tb      = ts_pval;
        ts      = 1'b0;
        t_start = 1'b0;
        t_end   = 1'b0;
        if (stb & drives) begin
            if (ext) begin                                    // shift edge to sampling edge
                if (t_act & e_sample)
                    t_end = 1'b1;                             // the monitor compares, below
                t_start = pending & (e_shift | ~cpha);
            end else if (t_act & (t_cnt != 5'd0)) begin
                t_cnt_s = t_cnt - 5'd1;
            end else if (t_act & ~t_half) begin               // middle of the bit
                t_end = 1'b1;                                 // the monitor compares, below
            end else begin                                    // idle, or the end of the bit
                t_act_s = 1'b0;
                t_start = pending;
            end
            if (t_end) begin
                if (t_mon & (rx_in != t_line)) begin          // monitor: quiet until TX on
                    t_err_s   = 1'b1;
                    tq_len_s  = 5'd0;
                    a_cnt_s   = 6'd0;
                    {tq_b_s, tq_s_s, tq_m_s, tq_c_s} = 64'd0;
                    t_act_s   = 1'b0;
                    ts_pend_s = 1'b0;
                    t_start   = 1'b0;
                end else if (ext) begin
                    t_act_s = 1'b0;
                end else begin
                    t_half_s = 1'b1;
                    t_cnt_s  = h1;
                    t_line_s = act(code, 1'b1, t_bit, t_p, t_line);
                end
            end
            if (t_start & ~(ext & t_act_s)) begin
                if (ts_pend) begin                            // the stuffed bit first
                    tb        = ts_pval;
                    ts_pend_s = 1'b0;
                    ts_run_s  = {4'd0, sequal};               // ends a run of 1s, starts one of equal bits
                    ts_last_s = ts_pval;
                end else begin
                    if (a_cnt != 6'd0) begin                  // the CRC's top bit (append)
                        tb       = crc_a[31] ^ crc_cfg[6];
                        ts       = a_s;
                        t_mon_s  = a_m;
                        a_feed   = 1'b1;
                        a_cnt_s  = a_cnt - 6'd1;
                    end else begin
                        tb       = tq_b[0];
                        ts       = tq_s[0];
                        t_mon_s  = tq_m[0];
                        tx_feed  = tq_c[0];
                        tq_b_s   = {1'b0, tq_b[15:1]};
                        tq_s_s   = {1'b0, tq_s[15:1]};
                        tq_m_s   = {1'b0, tq_m[15:1]};
                        tq_c_s   = {1'b0, tq_c[15:1]};
                        tq_len_s = tq_len - 5'd1;
                    end
                    if (ts) begin
                        ts_run_s  = (sequal ? (tb == ts_last) : tb) ? ts_run + 5'd1
                                                                     : {4'd0, sequal};
                        ts_last_s = tb;
                        ts_pend_s = ts_run_s >= slen;
                        ts_pval_s = sequal & ~tb;
                    end else begin
                        ts_run_s = 5'd0;                      // an unstuffed bit restarts the count
                    end
                end
                t_act_s  = 1'b1;
                t_half_s = 1'b0;
                t_cnt_s  = h1;
                t_bit_s  = tb;
                t_p_s    = t_last;
                t_last_s = tb;
                t_line_s = act(code, 1'b0, tb, t_last, t_line);
            end
        end
    end

    wire pending_s = (tq_len_s != 5'd0) | ts_pend_s | (a_cnt_s != 6'd0);
    wire drives_s  = ~t_err_s & (ctl[0] | t_act_s | pending_s);

    // ------------------------------------------------------------------ step 2: RX
    reg [15:0] rq_b_s;
    reg [4:0]  rq_len_s;
    reg [5:0]  r_cnt_s;
    reg        r_prev_s, r_armed_s, r_last_s, r_p_s;
    reg [7:0]  r_since_s;
    reg [8:0]  r_thr_s;
    reg [2:0]  r_run_s;
    reg        r_half_s, r_h0_s, r_end_s;
    reg [7:0]  r_quiet_s;
    reg [4:0]  rs_run_s;
    reg        rs_last_s, r_serr_s, r_ovr_s;
    reg        rb, rb_v;                   // a decoded bit
    reg        rx_feed;                    // it feeds the CRC
    reg        hv, hl;                     // a half-bit starts, its level (transition codes)

    wire       edge_in = rx_in != r_prev;
    wire [7:0] s1      = r_since + 8'd1;
    wire [8:0] t2      = {s1, 1'b0};
    // the halves the code gives bit b after the previous bit, which ended at end_b (at the
    // first edge after arming, the line before it: an ALIGN start decodes a bit at once)
    wire       end_b   = r_armed ? r_prev : r_end;
    wire       sym1_0  = act(code, 1'b0, 1'b1, r_p, end_b);
    wire       sym1_1  = act(code, 1'b1, 1'b1, r_p, sym1_0);
    wire       sym0_0  = act(code, 1'b0, 1'b0, r_p, end_b);
    wire       sym0_1  = act(code, 1'b1, 1'b0, r_p, sym0_0);

    always @(*) begin
        {rq_b_s, rq_len_s, r_cnt_s, r_since_s, r_quiet_s} = {rq_b, rq_len, r_cnt, r_since, r_quiet};
        {r_prev_s, r_armed_s, r_last_s, r_p_s} = {r_prev, r_armed, r_last, r_p};
        {r_thr_s, r_run_s, r_half_s, r_h0_s, r_end_s} = {r_thr, r_run, r_half, r_h0, r_end};
        {rs_run_s, rs_last_s, r_serr_s, r_ovr_s} = {rs_run, rs_last, r_serr, r_ovr};
        rb = act(code, 1'b0, 1'b1, r_p, r_last) == rx_in;     // a level code's sample
        rb_v = 1'b0;
        hv = 1'b0;
        hl = rx_in;
        if (stb & ctl[1]) begin
            r_prev_s  = rx_in;
            r_quiet_s = edge_in ? 8'd0 : r_quiet + {7'd0, ~&r_quiet};
            if (ext) begin                                    // a bit at each sampling edge
                if (e_sample) begin
                    rb_v     = 1'b1;
                    r_last_s = rx_in;
                end
            end else if (level_code) begin                    // sample mid-bit
                if (edge_in) begin
                    r_armed_s = 1'b0;
                    r_cnt_s   = {1'b0, h1};
                end else if (~r_armed) begin
                    if (r_cnt != 6'd0) begin
                        r_cnt_s = r_cnt - 6'd1;
                    end else begin
                        r_cnt_s  = {h1, 1'b1};            // 2H - 1
                        rb_v     = 1'b1;
                        r_last_s = rx_in;
                    end
                end
            end else if (r_armed) begin                       // transition codes: half-bits
                if (edge_in) begin
                    r_armed_s = 1'b0;
                    r_since_s = 8'd0;
                    r_thr_s   = {2'd0, h, 1'b0} + {3'd0, h};       // 3H
                    r_run_s   = 3'd1;
                    r_end_s   = r_prev;
                    r_h0_s    = r_prev;
                    hv        = 1'b1;
                    r_half_s  = align;                        // the half that starts: 1 if mid-bit
                end
            end else begin
                r_since_s = s1;
                if (edge_in) begin
                    r_since_s = 8'd0;
                    r_thr_s   = {2'd0, h, 1'b0} + {3'd0, h};
                    r_run_s   = 3'd1;
                    hv        = 1'b1;
                    r_half_s  = ~r_half;
                end else if (t2 >= r_thr) begin               // a half-bit without an edge
                    r_thr_s = r_thr + {2'd0, h, 1'b0};
                    r_run_s = r_run + 3'd1;
                    if (r_run_s > {1'b0, run} + 3'd2)
                        r_armed_s = 1'b1;                     // longer than the code's run: the end
                    else begin
                        hv       = 1'b1;
                        r_half_s = ~r_half;
                    end
                end
            end
            if (hv) begin                                     // a half-bit starts at hl
                if (~r_half_s) begin
                    r_h0_s = hl;
                end else if ({r_h0_s, hl} == {sym1_0, sym1_1}) begin
                    rb_v = 1'b1;
                    rb   = 1'b1;
                    r_end_s = hl;
                end else if ({r_h0_s, hl} == {sym0_0, sym0_1}) begin
                    rb_v = 1'b1;
                    rb   = 1'b0;
                    r_end_s = hl;
                end else begin
                    r_armed_s = 1'b1;                         // a code violation: the end
                end
            end
        end
        // a decoded bit: destuffing, the CRC and the queue
        rx_feed = 1'b0;
        if (rb_v) begin
            r_p_s = rb;
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
            if (rq_len == 5'd16)
                r_ovr_s = 1'b1;
            else begin
                rq_b_s   = rq_b | ({15'd0, rb} << rq_len[3:0]);
                rq_len_s = rq_len + 5'd1;
            end
        end
    end

    // ------------------------------------------------------------------ step 2: CRC
    wire [31:0] crc_s = a_feed ? {crc_a[30:0], 1'b0}                  // fed its own top bit
                      : (tx_feed | rx_feed) ? crc_step(crc_a, tx_feed ? tb : rb) : crc_a;

    // ------------------------------------------------------------------ status, outputs
    wire [1:0] pair = {pin_in1, pin_in};                    // the line state, raw
    assign status = {t_err_s, pair == 2'd3, pair == 2'd2, pair == 2'd1, pair == 2'd0,
                     r_ovr_s, r_serr_s, 1'b0, crc_s == cres,
                     rq_len_s != 5'd0, tq_len_s == 5'd0,
                     ~ctl[1] | (r_quiet_s >= {h, 2'b00}),
                     ~t_act_s & ~pending_s};
    assign status_r = {r_ovr, r_serr, 1'b0, crc_a == cres,
                       rq_len != 5'd0, tq_len == 5'd0,
                       ~ctl[1] | (r_quiet >= {h, 2'b00}),
                       ~t_act & ~pending};
    assign tx_on     = ctl[0];
    assign rx_on     = ctl[1];
    assign tx_len_s  = tq_len_s;
    assign rx_len_s  = rq_len_s;
    assign crc_busy  = a_cnt_s != 6'd0;
    assign rx_bits_s = rq_b_s;
    assign drive     = drives;
    assign line      = t_line ^ inv;
    assign bit_clk   = t_act ? cpol ^ cpha ^ t_half : cpol;
    assign crc       = crc_a;

    // ------------------------------------------------------------------ step 3 and registers
    wire [15:0] tx_mask  = ~(16'hFFFF << tx_n) << tq_len_s;
    wire        tx_rise  = ctl_we & ctl_val[0] & ~ctl[0];
    wire        rx_rise  = ctl_we & ctl_val[1] & ~ctl[1];

    always @(posedge clk) begin
        if (!rst_n || reset) begin
            ctl     <= 5'd0;
            baud    <= rst_n ? baud_rst : 5'd0;
            crc_a   <= rst_n ? cinit : 32'd0;
            {tq_b, tq_s, tq_m, tq_c} <= 64'd0;
            tq_len  <= 5'd0;
            {a_cnt, a_s, a_m} <= 8'd0;
            {t_act, t_half, t_bit, t_line, t_mon, t_err, t_p, t_last} <= 8'd0;
            t_cnt   <= 5'd0;
            {ts_run, ts_last, ts_pend, ts_pval} <= 8'd0;
            e_prev  <= rst_n & pin_in1;
            rq_b    <= 16'd0;
            rq_len  <= 5'd0;
            {r_prev, r_last, r_p} <= 3'd0;
            r_armed <= 1'b1;
            r_cnt   <= 6'd0;
            r_since <= 8'd0;
            r_thr   <= 9'd0;
            r_run   <= 3'd0;
            {r_half, r_h0, r_end} <= 3'd0;
            r_quiet <= 8'd0;
            {rs_run, rs_last, r_serr, r_ovr} <= 8'd0;
        end else begin
            {tq_b, tq_s, tq_m, tq_c, tq_len, a_cnt} <= {tq_b_s, tq_s_s, tq_m_s, tq_c_s, tq_len_s, a_cnt_s};
            {t_act, t_half, t_bit, t_line, t_mon, t_err} <= {t_act_s, t_half_s, t_bit_s, t_line_s, t_mon_s, t_err_s};
            {t_p, t_last} <= {t_p_s, t_last_s};
            {t_cnt, ts_run, ts_last, ts_pend, ts_pval} <= {t_cnt_s, ts_run_s, ts_last_s, ts_pend_s, ts_pval_s};
            {rq_b, rq_len, r_cnt, r_since, r_quiet} <= {rq_b_s, rq_len_s, r_cnt_s, r_since_s, r_quiet_s};
            {r_prev, r_armed, r_last, r_p} <= {r_prev_s, r_armed_s, r_last_s, r_p_s};
            {r_thr, r_run, r_half, r_h0, r_end} <= {r_thr_s, r_run_s, r_half_s, r_h0_s, r_end_s};
            {rs_run, rs_last, r_serr, r_ovr} <= {rs_run_s, rs_last_s, r_serr_s, r_ovr_s};
            crc_a <= feed ? crc_step(crc_s, feed_bit) : crc_s;
            if (stb)
                e_prev <= pin_in1;

            if (stb) begin
                if (ctl_we) begin
                    ctl <= ctl_val;
                    if (tx_rise) begin
                        t_err <= 1'b0;
                        if (~drives_s) begin                  // the encoder starts from the pin
                            t_line  <= pin_now ^ inv;
                            ts_run  <= 5'd0;
                            ts_pend <= 1'b0;
                            t_last  <= 1'b0;
                        end
                    end
                    if (rx_rise) begin                        // a new reception
                        rq_len  <= 5'd0;
                        rq_b    <= 16'd0;
                        r_armed <= 1'b1;
                        r_cnt   <= 6'd0;
                        r_since <= 8'd0;
                        r_quiet <= 8'd0;
                        r_prev  <= rx_in;
                        r_last  <= rx_in;
                        r_p     <= 1'b0;
                        {r_serr, r_ovr} <= 2'b00;
                    end
                    if (rx_rise | ~ctl_val[2])
                        {rs_run, rs_last} <= 6'd0;
                end
                if (baud_we)
                    baud <= baud_val;
                if (tx_push & ~t_err_s) begin
                    tq_b   <= tq_b_s | (tx_bits << tq_len_s);
                    tq_s   <= tq_s_s | (tx_mask & {16{ctl[2]}});
                    tq_m   <= tq_m_s | (tx_mask & {16{ctl[4]}});
                    tq_c   <= tq_c_s | (tx_mask & {16{ctl[3]}});
                    tq_len <= tq_len_s + tx_n;
                end
                if (rx_take) begin
                    rq_b   <= rq_b_s >> rx_n;
                    rq_len <= rq_len_s - rx_n;
                end
                if (crc_reset) begin
                    crc_a <= cinit;
                    {r_serr, r_ovr} <= 2'b00;
                end
                if (crc_append & ~t_err_s) begin
                    a_cnt <= {1'b0, crc_cfg[4:0]} + 6'd1;
                    a_s   <= ctl[2];
                    a_m   <= ctl[4];
                end
                if (tx_abort) begin
                    ctl[0]  <= 1'b0;
                    {tq_b, tq_s, tq_m, tq_c} <= 64'd0;
                    tq_len  <= 5'd0;
                    a_cnt   <= 6'd0;
                    t_act   <= 1'b0;
                    ts_pend <= 1'b0;
                end
            end
        end
    end

endmodule

`default_nettype wire
