// imem.v - an SM's instruction memory, 64 x 16: the flip-flop model of the IHP SRAM macro
// (imem_sram.v, same ports and timing) for RTL simulation, lockstep, formal and the FPGA.
//
// One port: a write stores wdata and holds rdata; otherwise rdata is the word at addr from
// the next clock (the SM presents its next PC, so rdata is always the word at its PC). The
// array and rdata are not reset, as in the macro: the host writes every word it runs.

`default_nettype none

module imem (
    input  wire        clk,
    input  wire [5:0]  addr,
    input  wire        we,
    input  wire [15:0] wdata,
    output reg  [15:0] rdata
);

    reg [15:0] mem [0:63];

    always @(posedge clk) begin
        if (we)
            mem[addr] <= wdata;
        else
            rdata <= mem[addr];
    end

endmodule

`default_nettype wire
