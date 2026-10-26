`default_nettype none
`timescale 1ns / 1ps

// cocotb testbench wrapper (Tiny Tapeout template): instantiates the chip and
// exposes its pins as plain regs/wires that test.py drives and checks.
// The same file is used for RTL and gate-level simulation (see Makefile).
module tb ();

  // Waveform dump: open tb.fst with GTKWave or Surfer
  initial begin
    $dumpfile("tb.fst");
    $dumpvars(0, tb);
    #1;
  end

  reg clk;
  reg rst_n;
  reg ena;
  reg [7:0] ui_in;
  reg [7:0] uio_in;
  wire [7:0] uo_out;
  wire [7:0] uio_out;
  wire [7:0] uio_oe;

  tt_um_theotacconi_openpio user_project (
      .ui_in  (ui_in),    // [0] SCK [1] MOSI [2] CS_N, [7:3] GPIO_IN
      .uo_out (uo_out),   // [0] MISO [1] IRQ, [7:2] GPIO_OUT
      .uio_in (uio_in),   // PIO pins: input path
      .uio_out(uio_out),  // PIO pins: output path
      .uio_oe (uio_oe),   // PIO pins: 1 = output
      .ena    (ena),
      .clk    (clk),
      .rst_n  (rst_n)
  );

endmodule
