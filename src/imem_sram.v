// imem_sram.v - openPIO instruction memory on the IHP SRAM macro (ASIC only)
//
// Drop-in for imem.v (same module name and ports), listed in info.yaml in
// its place. imem.v stays the model for RTL simulation, formal, lockstep and
// the FPGA build; the gate-level test simulates this file through IHP's
// behavioural model of the macro (test/Makefile, GATES=yes).
//
// Macro RM_IHPSG13_1P_64x16_c2 (PDK libs.ref/sg13cmos5l_sram): 64 x 16,
// single port, synchronous read. Only words 0..31 are used (A_ADDR[5] = 0).
//   A_REN & !A_WEN : A_DOUT <= mem[A_ADDR] at the edge (1 cycle, as imem.v)
//   A_WEN          : mem[A_ADDR] <= A_DIN, A_DOUT holds
//   neither        : A_DOUT holds (rd_en = 0 freezes the stalled instruction)
// Slow corner: A_CLK -> A_DOUT <= 5.3 ns, setup <= 1.0 ns (A_MEN/A_WEN).
//
// Differences from imem.v, both only while CTRL.EN = 0 (ISA.md: IMEM writes
// require EN = 0, and the core needs one en = 0 cycle after the last write):
//   - one port: a host write takes the cycle, the read waits for the next one
//     (imem.v reads and writes in the same cycle);
//   - A_DOUT has no reset: rd_valid masks it to 16'h0000 ("jmp 0", the reset
//     value of imem.v rd_data) until the first read after reset.

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
    output wire [15:0] rd_data
);

    wire        ren = rd_en & ~wr_en;   // write wins the single port
    wire [15:0] dout;
    reg         rd_valid;               // A_DOUT holds a word read since reset

    always @(posedge clk) begin
        if (!rst_n)
            rd_valid <= 1'b0;
        else if (ren)
            rd_valid <= 1'b1;
    end

    assign rd_data = rd_valid ? dout : 16'h0000;

    // Instance path after flattening: u_core.u_imem.u_sram (src/config.json)
    (* keep *)
    RM_IHPSG13_1P_64x16_c2 u_sram (
        .A_CLK  (clk),
        .A_MEN  (1'b1),
        .A_WEN  (wr_en),
        .A_REN  (ren),
        .A_ADDR ({1'b0, wr_en ? wr_addr : rd_addr}),
        .A_DIN  (wr_data),
        .A_DLY  (1'b1),                 // must be 1 (IHP model stops otherwise)
        .A_DOUT (dout)
    );

endmodule

`default_nettype wire
