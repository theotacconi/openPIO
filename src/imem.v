// imem.v - openPIO instruction memory, 32 x 16-bit
//
// Simple dual-port (1R + 1W), single clock:
//   - Write port: driven by the host config interface (program load).
//   - Read port : synchronous, registered output, used by the execution core.
//
// Read latency is one cycle: rd_data reflects mem[rd_addr] sampled on the
// previous rising edge where rd_en = 1. To fetch without bubbles the core
// should present its *next* PC on rd_addr, and drop rd_en to hold the current
// instruction word while stalled or delaying.
//
// Same-address read/write in one cycle returns the OLD word (read-first).
//
// The array is deliberately not reset so that it can infer LUT/BSRAM on
// Gowin and a plain DFF array under Yosys; the host must load all words
// it intends to execute (sim/model/emu.py starts from an all-zero image).
// rd_data itself is reset to 16'h0000 (= "jmp 0").

`default_nettype none

module imem (
    input  wire        clk,
    input  wire        rst_n,      // synchronous, active-low; clears rd_data only

    // Config write port
    input  wire        wr_en,
    input  wire [4:0]  wr_addr,
    input  wire [15:0] wr_data,

    // Core read port
    input  wire        rd_en,
    input  wire [4:0]  rd_addr,
    output reg  [15:0] rd_data
);

    reg [15:0] mem [0:31];

    always @(posedge clk) begin
        if (wr_en)
            mem[wr_addr] <= wr_data;
    end

    always @(posedge clk) begin
        if (!rst_n)
            rd_data <= 16'h0000;
        else if (rd_en)
            rd_data <= mem[rd_addr];
    end

endmodule

`default_nettype wire
