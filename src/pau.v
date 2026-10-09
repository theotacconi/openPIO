// pau.v - openPIO Protocol Acceleration Unit (OUT PINS line coders)
//
// Manchester TX (PAU_CFG[3]) -- mirrors emu.py pau_tx_queue:
//   Each data bit b becomes hp samples of b then hp samples of ~b
//   (hp = PAU_BAUD, 0 -> 1), one sample per SM strobe. emu.py stores the
//   expanded samples; here the queue is m_buf (pending data bits, head at
//   bit 0) + position (m_half, m_cnt) inside the head symbol. OUT appends
//   only when <= 1 symbol is pending (len <= 2*hp), so 17 bits suffice.
//   Per strobe, in order: pop one sample (core drives it onto pin out_base)
//   -> the instruction sees mtx_busy / mtx_full of the post-pop queue.
//   OUT into an empty queue: core drives sample 0 itself, the queue keeps
//   samples 1..2*hp*n-1 (position pre-advanced by one).
//   Limit: hp is read live, so PAU_BAUD must not change while the queue is
//   non-empty (emu.py keeps the old expansion).
//
// NRZI + bit-stuffing (PAU_CFG[0], PAU_CFG[1]) -- combinational over the
// OUT bits in transmit order: 0 toggles J/K, 1 holds; with stuffing, the 6th
// consecutive 1 adds a toggle. Start state is the current line (J = 01,
// K = 10) or, on SE0/SE1, the last NRZI state. ones saturates at 7 (> 6).

`default_nettype none

module pau (
    input  wire        clk,
    input  wire        rst_n,            // synchronous, active-low
    input  wire        step,             // SM strobe (en & prescaler tick)

    input  wire [4:0]  baud,             // PAU_BAUD
    input  wire        stuff_en,         // PAU_CFG[1]

    // OUT bits in transmit order (bit 0 first), zero above tx_n
    input  wire [15:0] tx_bits,
    input  wire [4:0]  tx_n,             // 1..16

    // Manchester TX queue
    input  wire        mtx_push,         // OUT PINS accepted (requires !mtx_full)
    input  wire        mtx_flush,        // SET PINS / SET PAU_CFG without bit 3
    output wire        mtx_pop,          // sample on pin this strobe
    output wire        mtx_pop_bit,
    output wire        mtx_busy,         // post-pop queue non-empty
    output wire        mtx_full,         // post-pop queue > 2*hp samples

    // NRZI encoder
    input  wire        nrzi_out,         // OUT PINS in NRZI mode
    input  wire [1:0]  nrzi_line_in,     // pins [out_base+1:out_base] after side-set
    input  wire        nrzi_load,        // SET PINS J/K
    input  wire        nrzi_load_k,
    output wire [1:0]  nrzi_line,        // new {D+, D-}

    output wire [4:0]  dbg_mtx_nsym,
    output wire        dbg_nrzi_k,
    output wire [2:0]  dbg_nrzi_ones
);

    // ---------------------------------------------------------------
    // Manchester TX queue
    // ---------------------------------------------------------------
    reg  [16:0] m_buf;
    reg  [4:0]  m_nsym;    // pending symbols incl. partially sent head
    reg         m_half;    // 0: first half (b), 1: second half (~b)
    reg  [4:0]  m_cnt;     // samples sent in current half

    wire [4:0] hp = (baud == 5'd0) ? 5'd1 : baud;

    assign mtx_pop     = step & (m_nsym != 5'd0);
    assign mtx_pop_bit = m_buf[0] ^ m_half;

    // Post-pop position
    wire [4:0] cnt_inc  = m_cnt + 5'd1;
    wire       half_end = (cnt_inc >= hp);
    wire       sym_end  = half_end & m_half;

    wire [16:0] p_buf  = (mtx_pop & sym_end)  ? (m_buf >> 1)        : m_buf;
    wire [4:0]  p_nsym = (mtx_pop & sym_end)  ? (m_nsym - 5'd1)     : m_nsym;
    wire        p_half = (mtx_pop & half_end) ? ~m_half             : m_half;
    wire [4:0]  p_cnt  = !mtx_pop             ? m_cnt
                       : half_end             ? 5'd0 : cnt_inc;

    assign mtx_busy = (p_nsym != 5'd0);
    assign mtx_full = (p_nsym > 5'd1);

    always @(posedge clk) begin
        if (!rst_n || mtx_flush) begin
            m_buf  <= 17'd0;
            m_nsym <= 5'd0;
            m_half <= 1'b0;
            m_cnt  <= 5'd0;
        end else if (mtx_push && !mtx_busy) begin
            // Sample 0 goes straight to the pin; queue resumes at sample 1
            m_buf  <= {1'b0, tx_bits};
            m_nsym <= tx_n;
            m_half <= (hp == 5'd1);
            m_cnt  <= (hp == 5'd1) ? 5'd0 : 5'd1;
        end else if (mtx_push) begin
            // One partial symbol pending: append behind it
            m_buf  <= {tx_bits, p_buf[0]};
            m_nsym <= p_nsym + tx_n;
            m_half <= p_half;
            m_cnt  <= p_cnt;
        end else begin
            m_buf  <= p_buf;
            m_nsym <= p_nsym;
            m_half <= p_half;
            m_cnt  <= p_cnt;
        end
    end

    // ---------------------------------------------------------------
    // NRZI + bit-stuffing
    // ---------------------------------------------------------------
    reg        nrzi_k;     // 0 = J (01), 1 = K (10)
    reg  [2:0] ones;

    reg        k_c;
    reg  [2:0] ones_c;
    integer    i;

    always @(*) begin
        k_c    = (nrzi_line_in == 2'b10) ? 1'b1
               : (nrzi_line_in == 2'b01) ? 1'b0 : nrzi_k;
        ones_c = ones;
        for (i = 0; i < 16; i = i + 1) begin
            if (i < tx_n) begin
                if (!tx_bits[i]) begin
                    k_c    = ~k_c;
                    ones_c = 3'd0;
                end else begin
                    if (ones_c != 3'd7)
                        ones_c = ones_c + 3'd1;
                    if (stuff_en && ones_c == 3'd6) begin
                        k_c    = ~k_c;
                        ones_c = 3'd0;
                    end
                end
            end
        end
    end

    always @(posedge clk) begin
        if (!rst_n) begin
            nrzi_k <= 1'b0;
            ones   <= 3'd0;
        end else if (nrzi_load) begin
            nrzi_k <= nrzi_load_k;
            ones   <= 3'd0;
        end else if (nrzi_out) begin
            nrzi_k <= k_c;
            ones   <= ones_c;
        end
    end

    assign nrzi_line     = k_c ? 2'b10 : 2'b01;
    assign dbg_mtx_nsym  = m_nsym;
    assign dbg_nrzi_k    = nrzi_k;
    assign dbg_nrzi_ones = ones;

endmodule

`default_nettype wire
