// fifo.v - the TX and RX FIFOs of one state machine (ISA.md, FIFOs; emu.py buffer_clock).
//
//   TX: host -> IN register -> SRAM ring (8 n) -> front (4) -> SM (PULL)
//   RX: SM (PUSH) -> front (4) -> SRAM ring (256 - 8 n) -> OUT register -> host
//
// The data SRAM (256 x 16, one port, a read's word a clock later) is outside: TX ring at
// 0..8n-1, RX ring at 8n..255, n = FIFO_SPLIT (1..31, 0 = 16) as at the last RESET; the ring
// pointers are SRAM addresses. At the end of each clock, after the SM's PULL / PUSH: the
// word read in the clock before lands (TX front or OUT register), then the port serves one
// request, the first pending after the one served last: TX refill, TX store, RX store, RX
// fetch. The host's RX_POP takes the OUT register at the clock start; its TX_PUSH lands at the
// end, into the IN register if that was free at the clock start. CTRL.RESET (reset) empties
// everything at the end of the clock.

`default_nettype none

module fifo (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        reset,         // CTRL.RESET of this SM

    // SM side: the fronts as at the clock start
    output wire        tx_empty,      // TX front
    output wire [15:0] tx_data,
    input  wire        tx_pop,
    output wire        rx_full,       // RX front
    input  wire        rx_push,
    input  wire [15:0] rx_data,
    output wire [7:0]  tx_level,      // the whole FIFOs, 0..253
    output wire [7:0]  rx_level,
    output wire        rx_fifo_full,  // the whole RX FIFO
    input  wire [4:0]  split,         // FIFO_SPLIT, taken on RESET

    // host side
    input  wire        h_push,        // TX_PUSH
    input  wire [15:0] h_wdata,
    output wire        h_room,        // the IN register is free: a TX_PUSH lands
    input  wire        h_pop,         // RX_POP
    output wire [15:0] h_rdata,       // the OUT register (0 when empty)

    // data SRAM port
    output reg  [7:0]  ram_addr,
    output reg         ram_we,
    output reg         ram_re,
    output reg  [15:0] ram_wdata,
    input  wire [15:0] ram_rdata
);

    reg [63:0] txf;                   // TX front, 4 words: head txh, count txn
    reg [1:0]  txh;
    reg [2:0]  txn;
    reg [63:0] rxf;
    reg [1:0]  rxh;
    reg [2:0]  rxn;
    reg [15:0] in_w, out_w;
    reg        in_v, out_v;
    reg [4:0]  sp;                               // the TX ring in 8 words, 1..31
    reg [7:0]  txr_rd, txr_wr, rxr_rd, rxr_wr;   // ring pointers, SRAM addresses
    reg [7:0]  txr_n, rxr_n;                     // ring counts, 0..248
    reg        tx_fl, rx_fl;                     // an SRAM read lands at the end of this clock
    reg [1:0]  turn;                             // the request served first

    assign tx_empty = txn == 3'd0;
    assign tx_data  = txf[16*txh +: 16];
    assign rx_full  = rxn == 3'd4;
    assign tx_level = {5'd0, txn} + txr_n + {7'd0, tx_fl} + {7'd0, in_v};
    assign rx_level = {5'd0, rxn} + rxr_n + {7'd0, rx_fl} + {7'd0, out_v};

    wire [7:0] tx_size = {sp, 3'd0};             // TX ring: SRAM 0 .. tx_size - 1
    wire [7:0] rx_size = 8'd0 - tx_size;         // RX ring: SRAM tx_size .. 255
    assign rx_fifo_full = rx_level == rx_size + 8'd5;
    wire [4:0] sp_new  = (!rst_n || split == 5'd0) ? 5'd16 : split;
    wire [7:0] txr_rd1 = txr_rd + 8'd1, txr_wr1 = txr_wr + 8'd1;
    wire [7:0] txr_rd_n = txr_rd1 == tx_size ? 8'd0 : txr_rd1;       // the next, round the ring
    wire [7:0] txr_wr_n = txr_wr1 == tx_size ? 8'd0 : txr_wr1;
    wire [7:0] rxr_rd_n = rxr_rd == 8'hFF ? tx_size : rxr_rd + 8'd1;
    wire [7:0] rxr_wr_n = rxr_wr == 8'hFF ? tx_size : rxr_wr + 8'd1;
    assign h_room   = ~in_v;
    assign h_rdata  = out_v ? out_w : 16'd0;

    wire [1:0] tx_tail = txh + txn[1:0];         // where the landing word goes
    wire [1:0] rx_tail = rxh + rxn[1:0];         // where the SM's word goes

    // the fronts after the SM and the landing word
    wire [2:0] txn_sm  = txn - {2'd0, tx_pop};
    wire [2:0] txn_l   = txn_sm + {2'd0, tx_fl};
    wire [2:0] rxn_sm  = rxn + {2'd0, rx_push};
    wire       out_l   = (out_v & ~h_pop) | rx_fl;

    // pending requests, by number: 0 TX refill, 1 TX store, 2 RX store, 3 RX fetch
    wire [3:0] pend = {(rxr_n != 8'd0) & ~out_l, (rxn_sm != 3'd0) & (rxr_n != rx_size),
                       in_v & (txr_n != tx_size), (txr_n != 8'd0) & (txn_l != 3'd4)};
    wire [7:0] pend_x = {pend, pend};
    wire [3:0] pend2  = pend_x[{1'b0, turn} +: 4];  // pending, from the turn on
    reg  [1:0] req;
    reg        serve;
    always @(*) begin
        serve = |pend;
        casez (pend2)
            4'b???1: req = turn;
            4'b??10: req = turn + 2'd1;
            4'b?100: req = turn + 2'd2;
            default: req = turn + 2'd3;
        endcase
        ram_we    = serve & (req == 2'd1 | req == 2'd2);
        ram_re    = serve & (req == 2'd0 | req == 2'd3);
        case (req)
            2'd0:    ram_addr = txr_rd;
            2'd1:    ram_addr = txr_wr;
            2'd2:    ram_addr = rxr_wr;
            default: ram_addr = rxr_rd;
        endcase
        ram_wdata = req == 2'd1 ? in_w : (rxn == 3'd0 ? rx_data : rxf[16*rxh +: 16]);
    end
    wire do_refill = serve & req == 2'd0;
    wire do_txst   = serve & req == 2'd1;
    wire do_rxst   = serve & req == 2'd2;
    wire do_fetch  = serve & req == 2'd3;

    always @(posedge clk) begin
        if (!rst_n || reset) begin
            {txf, rxf} <= 128'd0;
            {txh, rxh} <= 4'd0;
            {txn, rxn} <= 6'd0;
            {in_v, out_v, tx_fl, rx_fl} <= 4'd0;
            sp <= sp_new;
            {txr_rd, txr_wr} <= 16'd0;
            {rxr_rd, rxr_wr} <= {2{sp_new, 3'd0}};
            {txr_n, rxr_n} <= 16'd0;
            turn <= 2'd0;
            in_w <= 16'd0;
            out_w <= 16'd0;
        end else begin
            // TX front: the SM pops the head, the SRAM word lands at the tail (the pop leaves
            // it in place; the read was issued with room, so txn < 4)
            if (tx_fl)
                txf[16*tx_tail +: 16] <= ram_rdata;
            txh <= txh + {1'b0, tx_pop};
            txn <= txn_l;
            // RX front: the SM pushes at the tail, a store takes the head
            if (rx_push)
                rxf[16*rx_tail +: 16] <= rx_data;
            rxh <= rxh + {1'b0, do_rxst};
            rxn <= rxn_sm - {2'd0, do_rxst};
            // IN and OUT registers
            if (do_txst)
                in_v <= 1'b0;
            if (h_push & ~in_v) begin
                in_v <= 1'b1;
                in_w <= h_wdata;
            end
            if (h_pop)
                out_v <= 1'b0;
            if (rx_fl) begin
                out_v <= 1'b1;
                out_w <= ram_rdata;
            end
            // rings
            tx_fl <= do_refill;
            rx_fl <= do_fetch;
            if (do_refill) txr_rd <= txr_rd_n;
            if (do_txst)   txr_wr <= txr_wr_n;
            if (do_rxst)   rxr_wr <= rxr_wr_n;
            if (do_fetch)  rxr_rd <= rxr_rd_n;
            txr_n <= txr_n + {7'd0, do_txst} - {7'd0, do_refill};
            rxr_n <= rxr_n + {7'd0, do_rxst} - {7'd0, do_fetch};
            if (serve)
                turn <= req + 2'd1;
        end
    end

`ifdef FORMAL
    // test/formal/fifo.sby: after a chip reset, for every host and SM sequence and any SRAM
    // that keeps its contract, every word pushed comes out once, in order, unchanged, and a
    // word read from the SRAM has room where it lands. One word of each FIFO, a free choice,
    // is tracked: f_tpos / f_rpos count the words ahead of it, and its place follows. TX,
    // oldest first: front, the read in flight, ring, IN register; RX: OUT register, the read
    // in flight, ring, front.
    reg f_past = 1'b0;
    always @(posedge clk) f_past <= 1'b1;

    // the SRAM's contract: a read gives, the clock after, the word last written there
    reg [15:0] f_mem [0:255];
    reg        f_rd;
    reg [7:0]  f_ra;
    always @(posedge clk) begin
        if (ram_we)
            f_mem[ram_addr] <= ram_wdata;
        f_rd <= ram_re;
        f_ra <= ram_addr;
    end
    always @(*) begin
        if (f_rd) assume (ram_rdata == f_mem[f_ra]);
        assume (!(tx_pop & tx_empty));                   // the SM pops a word,
        assume (!(rx_push & rx_full));                   // pushes into room
    end

    wire       f_pick_tx = $anyseq, f_pick_rx = $anyseq;
    reg        f_ton, f_ron;
    reg [7:0]  f_tpos, f_rpos;
    reg [15:0] f_tval, f_rval;
    wire       f_rtake = h_pop & out_v;
    always @(posedge clk) begin
        if (!rst_n || reset) begin
            {f_ton, f_ron} <= 2'b00;
        end else begin
            if (f_ton) begin
                f_tpos <= f_tpos - {7'd0, tx_pop};
                if (tx_pop & f_tpos == 8'd0)
                    f_ton <= 1'b0;
            end else if (h_push & ~in_v & f_pick_tx) begin
                {f_ton, f_tval, f_tpos} <= {1'b1, h_wdata, tx_level - {7'd0, tx_pop}};
            end
            if (f_ron) begin
                f_rpos <= f_rpos - {7'd0, f_rtake};
                if (f_rtake & f_rpos == 8'd0)
                    f_ron <= 1'b0;
            end else if (rx_push & f_pick_rx) begin
                {f_ron, f_rval, f_rpos} <= {1'b1, rx_data, rx_level - {7'd0, f_rtake}};
            end
        end
    end

    wire [7:0] f_tk = f_tpos - {5'd0, txn} - {7'd0, tx_fl};       // TX: ring index, then IN
    wire [7:0] f_rk = f_rpos - {7'd0, out_v} - {7'd0, rx_fl};     // RX: ring index,
    wire [7:0] f_rj = f_rk - rxr_n;                               // then front
    wire [1:0] f_ti = txh + f_tpos[1:0], f_ri = rxh + f_rj[1:0];  // front slots
    // the SRAM address k words past a ring pointer (k at most the ring's size)
    function [7:0] f_tx_at(input [7:0] p, input [7:0] k);
        reg [8:0] a;
        begin
            a = {1'b0, p} + {1'b0, k};
            f_tx_at = a >= {1'b0, tx_size} ? a[7:0] - tx_size : a[7:0];   // mod 256: a < 496
        end
    endfunction
    function [7:0] f_rx_at(input [7:0] p, input [7:0] k);
        reg [8:0] a;
        begin
            a = {1'b0, p} + {1'b0, k};
            f_rx_at = a[8] ? a[7:0] + tx_size : a[7:0];
        end
    endfunction
    always @(*) if (f_past) begin
        assert (sp != 5'd0);
        assert (txn <= 3'd4 && rxn <= 3'd4 && txr_n <= tx_size && rxr_n <= rx_size);
        assert (txr_rd < tx_size && txr_wr < tx_size && rxr_rd >= tx_size && rxr_wr >= tx_size);
        assert (txr_wr == f_tx_at(txr_rd, txr_n) && rxr_wr == f_rx_at(rxr_rd, rxr_n));
        assert (tx_level <= tx_size + 8'd5 && rx_level <= rx_size + 8'd5);
        assert (!tx_fl || txn != 3'd4);                  // a TX read lands with room
        assert (!rx_fl || !out_v);                       // an RX read lands in a free OUT
        if (tx_fl) assert (f_rd && f_tx_at(f_ra, 8'd1) == txr_rd);
        if (rx_fl) assert (f_rd && f_rx_at(f_ra, 8'd1) == rxr_rd && f_ra >= tx_size);
        if (f_ton) begin
            assert (f_tpos < tx_level);
            if (f_tpos < {5'd0, txn})
                assert (txf[16*f_ti +: 16] == f_tval);
            else if (tx_fl && f_tpos == {5'd0, txn})
                assert (ram_rdata == f_tval);
            else if (f_tk < txr_n)
                assert (f_mem[f_tx_at(txr_rd, f_tk)] == f_tval);
            else
                assert (in_v && in_w == f_tval);
            if (f_tpos == 8'd0 && !tx_empty)
                assert (tx_data == f_tval);                       // the SM's next word
        end
        if (f_ron) begin
            assert (f_rpos < rx_level);
            if (f_rpos == 8'd0 && out_v)
                assert (h_rdata == f_rval);                       // out through RX_POP
            else if (rx_fl && f_rpos == {7'd0, out_v})
                assert (ram_rdata == f_rval);
            else if (f_rk < rxr_n)
                assert (f_mem[f_rx_at(rxr_rd, f_rk)] == f_rval);
            else
                assert (f_rj < {5'd0, rxn} && rxf[16*f_ri +: 16] == f_rval);
        end
    end
`endif

endmodule

`default_nettype wire
