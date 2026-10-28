// fifo_ram.v - an SM's FIFO data memory, 256 x 16 (TX ring 0..127, RX ring 128..255): the
// flip-flop model of the IHP SRAM macro (fifo_ram_sram.v, same ports and timing).
//
// One port: a write stores wdata, a read gives the word at addr from the next clock; rdata holds
// otherwise. Not reset, as the macro: fifo.v reads only words it wrote.

`default_nettype none

module fifo_ram (
    input  wire        clk,
    input  wire [7:0]  addr,
    input  wire        we,
    input  wire        re,
    input  wire [15:0] wdata,
    output reg  [15:0] rdata
);

    reg [15:0] mem [0:255];

    always @(posedge clk) begin
        if (we)
            mem[addr] <= wdata;
        else if (re)
            rdata <= mem[addr];
    end

endmodule

`default_nettype wire
