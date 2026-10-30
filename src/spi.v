// spi.v - the host's SPI slave (HOST.md, SPI): mode 0, MSB first, SCLK, MOSI and CS_N sampled by
// the system clock through 2-flip-flop synchronizers (SCLK high and low for 3 clocks or more).
//
// A transaction: CS_N falls, a 16-bit header, data words, CS_N rises. Each data word is one host
// access of the chip core (openpio.v), made in the clock its last bit is seen (a write: the word
// just in) or, for a read, when the word before it ends: its MSB must be on MISO before the
// master clocks the word. RX_POP reads only its Addr words (0 = 64): a read made for a word the
// master never clocks then has no side effect. MISO comes from a flip-flop: the read word (an
// IMEM_RD's a clock later, from the SRAM), else the STATUS of the transaction's SM, a clock after
// the word before ends; during the header, that of the previous transaction's SM, at CS_N's fall.
// IMEM_WR and IMEM_RD take their address in the first data word (no access for it).

`default_nettype none

module spi (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        sclk,
    input  wire        mosi,
    input  wire        cs_n,
    output wire        miso,

    output reg         h_valid,       // a host access this clock (openpio.v)
    output reg  [2:0]  h_op,
    output reg  [1:0]  h_sm,
    output reg  [5:0]  h_addr,        // a register, or an IMEM word
    output wire [15:0] h_wdata,
    input  wire [15:0] h_rdata,
    input  wire [15:0] h_imem_rdata,  // an IMEM_RD's word, the clock after it
    input  wire [15:0] h_status       // STATUS of SM h_sm
);

    localparam OP_REG_WR = 3'd2, OP_REG_RD = 3'd3, OP_TX_PUSH = 3'd4, OP_RX_POP = 3'd5,
               OP_IMEM_WR = 3'd6, OP_IMEM_RD = 3'd7;

    reg [2:0]  s_sclk, s_cs;              // synchronizers, and [2] for the edges
    reg [1:0]  s_mosi;
    reg [14:0] sr;                        // the bits of the word so far
    reg [3:0]  bits;                      // bits in
    reg        hdr;                       // the header is in
    reg [2:0]  op;
    reg [1:0]  sm, last_sm;               // this transaction's SM, the previous one's
    reg [5:0]  addr;
    reg        ia;                        // the next word is an IMEM address
    reg [6:0]  pops;                      // RX_POP words left
    reg [15:0] out;                       // MISO shift register
    reg        load_next;                 // load MISO next clock: STATUS, or the IMEM word
    reg        load_imem;

    wire rise = s_sclk[1] & ~s_sclk[2] & ~s_cs[1];
    wire fall_cs = ~s_cs[1] & s_cs[2];
    wire done = rise & bits == 4'd15;     // a word is in
    wire [15:0] w = {sr, s_mosi[1]};

    // the transaction after this word: the header's fields when it is the header
    wire [2:0] op_n   = hdr ? op : w[15:13];
    wire [1:0] sm_n   = hdr ? sm : w[12:11];
    wire       read_n = op_n == OP_REG_RD | op_n == OP_RX_POP | op_n == OP_IMEM_RD;
    wire       write  = hdr & ~ia & (op == OP_REG_WR | op == OP_TX_PUSH | op == OP_IMEM_WR);
    wire       imem_n = op_n == OP_IMEM_WR | op_n == OP_IMEM_RD;
    wire [5:0] addr_r = (~hdr | ia) ? w[5:0] : addr + 6'd1;    // the next read's
    wire [6:0] pops_n = hdr ? pops : {w[5:0] == 6'd0, w[5:0]};
    wire       read   = done & read_n & (op_n != OP_RX_POP | pops_n != 7'd0) & (hdr | ~imem_n);

    assign h_wdata = w;
    assign miso    = out[15];

    always @(*) begin
        h_valid = 1'b0;
        h_op    = op_n;
        h_sm    = (s_cs[1] | fall_cs) ? last_sm : sm_n;
        h_addr  = addr;
        if (done & write) begin
            h_valid = 1'b1;
        end else if (read) begin
            h_valid = 1'b1;
            h_addr  = addr_r;
        end
    end

    always @(posedge clk) begin
        if (!rst_n) begin
            {s_sclk, s_mosi, s_cs} <= 8'b000_00_111;
            sr <= 15'd0;
            bits <= 4'd0;
            hdr <= 1'b0;
            {op, sm, last_sm, addr, pops} <= 20'd0;
            ia <= 1'b0;
            out <= 16'd0;
            {load_next, load_imem} <= 2'b00;
        end else begin
            s_sclk <= {s_sclk[1:0], sclk};
            s_mosi <= {s_mosi[0], mosi};
            s_cs   <= {s_cs[1:0], cs_n};
            load_next <= 1'b0;
            if (load_next)
                out <= load_imem ? h_imem_rdata : h_status;
            if (s_cs[1]) begin                                // idle
                bits <= 4'd0;
                hdr  <= 1'b0;
            end else if (fall_cs) begin
                out <= h_status;                              // the previous transaction's SM
            end else if (rise) begin
                sr   <= w[14:0];
                bits <= bits + 4'd1;
                out  <= {out[14:0], 1'b0};
                if (done) begin
                    if (!hdr) begin
                        {hdr, op, sm, last_sm} <= {1'b1, w[15:13], w[12:11], w[12:11]};
                        addr <= w[5:0];
                        ia   <= imem_n;
                    end else if (ia) begin                // the IMEM address
                        addr <= w[5:0];
                        ia   <= 1'b0;
                    end else if (write)
                        addr <= addr + 6'd1;
                    if (read) begin
                        addr <= addr_r;
                        pops <= pops_n - {6'd0, op_n == OP_RX_POP};
                        if (op_n == OP_IMEM_RD) begin
                            load_next <= 1'b1;
                            load_imem <= 1'b1;
                        end else
                            out <= h_rdata;
                    end else if (read_n & op_n == OP_RX_POP) begin  // RX_POP beyond its words
                        out <= 16'd0;
                        pops <= pops_n;
                    end else begin
                        load_next <= 1'b1;                    // STATUS, after a write lands
                        load_imem <= 1'b0;
                    end
                end
            end
        end
    end

endmodule

`default_nettype wire
