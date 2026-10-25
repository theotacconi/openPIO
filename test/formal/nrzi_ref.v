// nrzi_ref.v - reference NRZI + bit-stuffing encoder: the per-bit loop that
// src/pau.v replaced with a parallel form (one ripple stage per OUT bit, in
// emu.py order). Equivalence target for nrzi_equiv.sby only.

`default_nettype none

module nrzi_ref (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        stuff_en,
    input  wire [15:0] tx_bits,
    input  wire [4:0]  tx_n,
    input  wire        nrzi_out,
    input  wire [1:0]  nrzi_line_in,
    input  wire        nrzi_load,
    input  wire        nrzi_load_k,
    output wire [1:0]  nrzi_line,
    output wire        dbg_nrzi_k,
    output wire [2:0]  dbg_nrzi_ones
);

    reg        nrzi_k;
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
    assign dbg_nrzi_k    = nrzi_k;
    assign dbg_nrzi_ones = ones;

endmodule

`default_nettype wire
