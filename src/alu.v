// alu.v - openPIO 16-bit ALU (opcode 101)
//
// Purely combinational. Operand selection (PINS/X/Y/NULL/ISR/OSR/STATUS) and
// destination write-back live in the datapath; this block only computes the
// result, mirroring StateMachine.step() OP_ALU in sim/model/emu.py:
//
//   op = 00  MOV  y = b
//   op = 01  XOR  y = a ^ b
//   op = 10  ADD  y = a + b        (mod 2^16, no carry out)
//   op = 11  REV  y = bitrev16(b)  (source operand only)
//
// a = current destination value, b = selected source value.

`default_nettype none

module alu (
    input  wire [1:0]  op,   // instr[4:3]
    input  wire [15:0] a,    // destination operand
    input  wire [15:0] b,    // source operand
    output reg  [15:0] y
);

    localparam OP_MOV = 2'b00;
    localparam OP_XOR = 2'b01;
    localparam OP_ADD = 2'b10;
    localparam OP_REV = 2'b11;

    wire [15:0] b_rev;

    genvar i;
    generate
        for (i = 0; i < 16; i = i + 1) begin : g_rev
            assign b_rev[i] = b[15 - i];
        end
    endgenerate

    always @(*) begin
        case (op)
            OP_MOV:  y = b;
            OP_XOR:  y = a ^ b;
            OP_ADD:  y = a + b;
            default: y = b_rev;  // OP_REV
        endcase
    end

endmodule

`default_nettype wire
