// fifo.v - the TX and RX FIFOs of one state machine (ISA.md, FIFOs; emu.py buffer_clock).
//
//   TX: host -> IN register -> SRAM ring (128) -> front (4) -> SM (PULL)
//   RX: SM (PUSH) -> front (4) -> SRAM ring (128) -> OUT register -> host
//
// The data SRAM (256 x 16, one port, a read's word a clock later) is outside: TX ring at
// 0..127, RX ring at 128..255. At the end of each clock, after the SM's PULL / PUSH: the
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
    output wire [7:0]  tx_level,      // the whole FIFOs, 0..133
    output wire [7:0]  rx_level,

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
    reg [6:0]  txr_rd, txr_wr, rxr_rd, rxr_wr;   // ring pointers
    reg [7:0]  txr_n, rxr_n;                     // ring counts, 0..128
    reg        tx_fl, rx_fl;                     // an SRAM read lands at the end of this clock
    reg [1:0]  turn;                             // the request served first

    assign tx_empty = txn == 3'd0;
    assign tx_data  = txf[16*txh +: 16];
    assign rx_full  = rxn == 3'd4;
    assign tx_level = {5'd0, txn} + txr_n + {7'd0, tx_fl} + {7'd0, in_v};
    assign rx_level = {5'd0, rxn} + rxr_n + {7'd0, rx_fl} + {7'd0, out_v};
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
    wire [3:0] pend = {(rxr_n != 8'd0) & ~out_l, (rxn_sm != 3'd0) & ~rxr_n[7],
                       in_v & ~txr_n[7], (txr_n != 8'd0) & (txn_l != 3'd4)};
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
            2'd0:    ram_addr = {1'b0, txr_rd};
            2'd1:    ram_addr = {1'b0, txr_wr};
            2'd2:    ram_addr = {1'b1, rxr_wr};
            default: ram_addr = {1'b1, rxr_rd};
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
            {txr_rd, txr_wr, rxr_rd, rxr_wr} <= 28'd0;
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
            if (do_refill) txr_rd <= txr_rd + 7'd1;
            if (do_txst)   txr_wr <= txr_wr + 7'd1;
            if (do_rxst)   rxr_wr <= rxr_wr + 7'd1;
            if (do_fetch)  rxr_rd <= rxr_rd + 7'd1;
            txr_n <= txr_n + {7'd0, do_txst} - {7'd0, do_refill};
            rxr_n <= rxr_n + {7'd0, do_rxst} - {7'd0, do_fetch};
            if (serve)
                turn <= req + 2'd1;
        end
    end

endmodule

`default_nettype wire
