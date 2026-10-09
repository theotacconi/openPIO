// shifter.v - openPIO ISR / OSR shift registers with bit counters
//
// Holds the 16-bit Input Shift Register (ISR) and Output Shift Register (OSR)
// together with their 0..16 bit counters. Behaviour mirrors sim/model/emu.py:
//
//   IN  src, n   isr     <= dir ? (isr >> n) | (v << (16-n))   (shift right)
//                               : (isr << n) | v[n-1:0]         (shift left)
//                isr_cnt <= min(16, isr_cnt + n)
//
//   OUT dst, n   data    =  dir ? osr[n-1:0] : osr[15:16-n]
//                osr     <= dir ? osr >> n   : osr << n
//                osr_cnt <= min(16, osr_cnt + n)
//
//   Loads (whole-register writes, take priority over a shift):
//     ISR: PUSH / PUSH-noblock-drop -> (0, 0)
//          OUT ISR, n               -> (osr_shift_data, n)
//          ALU dest ISR             -> (result, 16)
//     OSR: PULL (FIFO or X), ALU dest OSR -> osr_cnt <= 0
//
// n is the raw 4-bit instruction field: 1..15 = 1..15 bits, 0 = 16 bits.
// osr_cnt counts bits shifted *out*; osr_empty (cnt == 16) drives JMP !OSRE
// and PULL IfEmpty. isr_full (cnt == 16) drives PUSH IfFull.

`default_nettype none

module shifter (
    input  wire        clk,
    input  wire        rst_n,           // synchronous, active-low (TT style)

    // Static shift direction config (1 = shift right, 0 = shift left)
    input  wire        cfg_in_shr,
    input  wire        cfg_out_shr,

    // Bit count for IN / OUT, raw instr[3:0] encoding (0 = 16)
    input  wire [3:0]  shift_cnt,

    // ISR
    input  wire        isr_shift,       // IN: shift isr_shift_data into ISR
    input  wire [15:0] isr_shift_data,  // source value (masked internally)
    input  wire        isr_load,        // overwrite ISR and its counter
    input  wire [15:0] isr_load_data,
    input  wire [4:0]  isr_load_cnt,    // 0..16
    output reg  [15:0] isr,
    output reg  [4:0]  isr_cnt,
    output wire        isr_full,

    // OSR
    input  wire        osr_shift,       // OUT: shift ShiftCount bits out
    input  wire        osr_load,        // overwrite OSR, counter <= 0
    input  wire [15:0] osr_load_data,
    output reg  [15:0] osr,
    output reg  [4:0]  osr_cnt,
    output wire        osr_empty,
    output wire [15:0] osr_shift_data   // bits OUT emits this cycle, LSB-aligned
);

    // Decoded bit count n = 1..16 and its complement 16-n = 0..15
    wire [4:0]  n     = {(shift_cnt == 4'd0), shift_cnt};
    wire [4:0]  n_inv = 5'd16 - n;
    wire [15:0] mask  = ~(16'hFFFF << n);

    // ---------------------------------------------------------------
    // ISR next-state
    // ---------------------------------------------------------------
    wire [15:0] isr_in_val = isr_shift_data & mask;
    wire [15:0] isr_shr    = (isr >> n) | (isr_in_val << n_inv);
    wire [15:0] isr_shl    = (isr << n) | isr_in_val;
    wire [5:0]  isr_sum    = {1'b0, isr_cnt} + {1'b0, n};
    wire [4:0]  isr_cnt_sat = (isr_sum > 6'd16) ? 5'd16 : isr_sum[4:0];

    always @(posedge clk) begin
        if (!rst_n) begin
            isr     <= 16'd0;
            isr_cnt <= 5'd0;
        end else if (isr_load) begin
            isr     <= isr_load_data;
            isr_cnt <= isr_load_cnt;
        end else if (isr_shift) begin
            isr     <= cfg_in_shr ? isr_shr : isr_shl;
            isr_cnt <= isr_cnt_sat;
        end
    end

    assign isr_full = isr_cnt[4];

    // ---------------------------------------------------------------
    // OSR next-state
    // ---------------------------------------------------------------
    wire [15:0] osr_shr    = osr >> n;
    wire [15:0] osr_shl    = osr << n;
    wire [5:0]  osr_sum    = {1'b0, osr_cnt} + {1'b0, n};
    wire [4:0]  osr_cnt_sat = (osr_sum > 6'd16) ? 5'd16 : osr_sum[4:0];

    assign osr_shift_data = cfg_out_shr ? (osr & mask) : (osr >> n_inv);

    always @(posedge clk) begin
        if (!rst_n) begin
            osr     <= 16'd0;
            osr_cnt <= 5'd0;
        end else if (osr_load) begin
            osr     <= osr_load_data;
            osr_cnt <= 5'd0;
        end else if (osr_shift) begin
            osr     <= cfg_out_shr ? osr_shr : osr_shl;
            osr_cnt <= osr_cnt_sat;
        end
    end

    assign osr_empty = osr_cnt[4];

endmodule

`default_nettype wire
