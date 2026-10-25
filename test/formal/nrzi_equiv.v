// nrzi_equiv.v - src/pau.v NRZI encoder == nrzi_ref.v for every input
// sequence after reset (all inputs free). Run: sby -f nrzi_equiv.sby

`default_nettype none

module nrzi_equiv (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        step,
    input  wire [4:0]  baud,
    input  wire        stuff_en,
    input  wire [15:0] tx_bits,
    input  wire [4:0]  tx_n,
    input  wire        mtx_push,
    input  wire        mtx_flush,
    input  wire        nrzi_out,
    input  wire [1:0]  nrzi_line_in,
    input  wire        nrzi_load,
    input  wire        nrzi_load_k
);

    wire [1:0] line, line_ref;
    wire       k, k_ref;
    wire [2:0] ones, ones_ref;

    pau u_pau (
        .clk           (clk),
        .rst_n         (rst_n),
        .step          (step),
        .baud          (baud),
        .stuff_en      (stuff_en),
        .tx_bits       (tx_bits),
        .tx_n          (tx_n),
        .mtx_push      (mtx_push),
        .mtx_flush     (mtx_flush),
        .mtx_pop       (),
        .mtx_pop_bit   (),
        .mtx_busy      (),
        .mtx_full      (),
        .nrzi_out      (nrzi_out),
        .nrzi_line_in  (nrzi_line_in),
        .nrzi_load     (nrzi_load),
        .nrzi_load_k   (nrzi_load_k),
        .nrzi_line     (line),
        .dbg_mtx_nsym  (),
        .dbg_nrzi_k    (k),
        .dbg_nrzi_ones (ones)
    );

    nrzi_ref u_ref (
        .clk           (clk),
        .rst_n         (rst_n),
        .stuff_en      (stuff_en),
        .tx_bits       (tx_bits),
        .tx_n          (tx_n),
        .nrzi_out      (nrzi_out),
        .nrzi_line_in  (nrzi_line_in),
        .nrzi_load     (nrzi_load),
        .nrzi_load_k   (nrzi_load_k),
        .nrzi_line     (line_ref),
        .dbg_nrzi_k    (k_ref),
        .dbg_nrzi_ones (ones_ref)
    );

    // First cycle is a reset; from then on state and output must match.
    reg init = 1'b1;
    always @(posedge clk) init <= 1'b0;

    always @(*) begin
        if (init) assume (!rst_n);
        else      assert ((line == line_ref) && (k == k_ref) && (ones == ones_ref));
    end

endmodule

`default_nettype wire
