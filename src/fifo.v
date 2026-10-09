// fifo.v - 4 x 16-bit synchronous FIFO, first-word-fall-through
//
// Depth 4 matches emu.py fifo_depth. rdata = head word whenever !empty.
// push while full is dropped unless a pop happens in the same cycle;
// pop while empty is ignored. Push and pop may coincide.

`default_nettype none

module fifo (
    input  wire        clk,
    input  wire        rst_n,      // synchronous, active-low; flushes
    input  wire        push,
    input  wire [15:0] wdata,
    input  wire        pop,
    output wire [15:0] rdata,
    output wire        empty,
    output wire        full
);

    reg [63:0] data;               // 4 words, word i at [16*i +: 16]
    reg [1:0]  wptr, rptr;
    reg [2:0]  count;              // 0..4

    wire do_pop  = pop  & ~empty;
    wire do_push = push & (~full | do_pop);

    always @(posedge clk) begin
        if (!rst_n) begin
            data  <= 64'd0;
            wptr  <= 2'd0;
            rptr  <= 2'd0;
            count <= 3'd0;
        end else begin
            if (do_push) begin
                data[{wptr, 4'b0000} +: 16] <= wdata;
                wptr <= wptr + 2'd1;
            end
            if (do_pop)
                rptr <= rptr + 2'd1;
            count <= count + {2'b00, do_push} - {2'b00, do_pop};
        end
    end

    assign rdata = data[{rptr, 4'b0000} +: 16];
    assign empty = (count == 3'd0);
    assign full  = count[2];

endmodule

`default_nettype wire
