// timing.v - ISA.md's first principle proved on the RTL (src/openpio_sm.v) for every input
// sequence after a chip reset: every instruction takes a known number of strobes. The checker
// reads the instruction as ISA.md encodes it and keeps its own count of the strobes; the RTL
// carries none of it (timing.sby exposes the signals the checker reads as ports of
// openpio_sm, for this wrapper alone). The RTL's strobe phases (issue, stall, delay) mark
// where an instruction starts and completes; what the checker proves is when they come.
// Run: sby -f timing.sby
//
//   1. An instruction that ISA.md gives no stall (JMP, taken or not, ALU, SET, IRQ that does
//      not wait, PUSH / PULL that do not block, IN / OUT of registers without Auto, TX abort,
//      the reserved encodings) completes on the strobe it issues.
//   2. After an instruction completes, the SM idles exactly delay x DLYMUL strobes (the
//      delay of the instruction's field, DLYMUL as SET left it) and the next instruction
//      issues on the strobe after: never sooner, never later.
//   3. A blocking PUSH, a blocking PULL and an IRQ wait complete on the first strobe that
//      finds room in the RX FIFO, a word in the TX FIFO, the flag clear: never later.
//   4. A timed WAIT that does not see its condition ends exactly TIMEOUT_VAL x DLYMUL strobes
//      after it issued (at least 1).
//
// Not here: when a WAIT's condition matches, and the stalls that wait for the PAU (IN / OUT
// PINS through its queues, CRC actions) or for autopush / autopull; the lockstep benches
// (test/lockstep/) hold those to emu.py clock for clock. Checked by mutation: a delay a strobe
// long, a taken JMP or an ALU operation that stalls, a PULL that ends late, DLYMUL applied a
// SET early, a timeout a strobe late: each fails the proof.

`default_nettype none

module timing_check (
    input wire        clk,
    input wire        rst_n,
    input wire        reset,
    input wire        en,
    input wire        h_we,
    input wire [5:0]  h_addr,
    input wire [15:0] instr,
    input wire [2:0]  ss_count,
    input wire        ss_opt,
    input wire        tx_empty,
    input wire        rx_full,
    input wire [7:0]  irq,
    input wire [7:0]  peer_irq,
    input wire [4:0]  tmo_val,
    // the RTL's own: the phase of this strobe, and its completion
    input wire        ph_exec,
    input wire        ph_stall,
    input wire        ph_delay,
    input wire        done,
    // state, for the induction: the checker's counts are the RTL's
    input wire        delaying,
    input wire        stalled,
    input wire        retry,
    input wire [9:0]  cnt,
    input wire [4:0]  dlymul
);

    // ------------------------------------------------------------ ISA.md's encoding
    wire [2:0] op      = instr[15:13];
    wire       jmp     = op[2:1] == 2'b00;
    wire       io      = op[2:1] == 2'b01;            // IN, OUT
    wire       alu     = op[2:1] == 2'b10;
    wire       ctl     = op == 3'b110;
    wire       set     = op == 3'b111;
    wire [1:0] typ     = instr[7:6];
    wire       irq_i   = ctl & typ == 2'd0;
    wire       irq_w   = irq_i & instr[4] & ~instr[5]; // set the flag, wait until it is cleared
    wire       fifo    = ctl & typ == 2'd1 & instr[2:0] == 3'd0;
    wire       pull_b  = fifo & instr[5] & instr[4] & ~instr[3];
    wire       push_b  = fifo & ~instr[5] & instr[4] & ~instr[3];
    wire       wait_t  = ctl & typ == 2'd2 & ~instr[2] & instr[3];
    wire       abort   = ctl & typ == 2'd3 & instr[5:4] == 2'd2 & instr[3:0] == 4'd0;
    wire       resv    = ctl & (typ == 2'd1 & instr[2:0] != 3'd0 | typ == 2'd2 & instr[2]
                                | typ == 2'd3 & instr[5:4] != 2'd3 & instr[3:0] != 4'd0);
    wire       reg_io  = io & ~instr[4] & instr[7:5] != 3'd0;
    wire       never   = jmp | alu | set | (irq_i & ~irq_w) | (fifo & ~instr[4]) | abort | resv | reg_io;

    // the delay: the field's low bits below the side-set ones (emu.py _decode_delay_sideset)
    wire [2:0] ss_bits = (ss_count == 3'd0) ? 3'd0 : ss_count + {2'd0, ss_opt};
    wire [4:0] delay   = instr[12:8] & (5'h1F >> ss_bits);
    wire       flag    = instr[3] ? peer_irq[instr[2:0]] : irq[instr[2:0]];

    // ------------------------------------------------------------ the checker's counts
    reg  [4:0] f_mul = 5'd1;          // DLYMUL: 1 after a reset, SET DLYMUL's value (0: 1)
    reg        f_idle = 1'b0;         // an instruction completed, the next has not issued
    reg  [9:0] f_left = 10'd0;        // ... and so many delay strobes are still to come
    reg        f_tw = 1'b0;           // a timed WAIT stalls
    reg  [9:0] f_tmo = 10'd0;         // ... and ends at the latest so many strobes on
    reg        f_past = 1'b0;
    wire [9:0] t_mul   = tmo_val * f_mul;
    wire       stb     = ph_exec | ph_stall | ph_delay;
    wire       lost    = !rst_n | reset | (h_we & ~en & h_addr == 6'h11);   // PC from the host

    always @(posedge clk) begin
        f_past <= 1'b1;
        if (lost) begin
            f_idle <= 1'b0;
            f_tw <= 1'b0;
        end else if (stb) begin
            if (done) begin
                f_idle <= 1'b1;
                f_left <= delay * f_mul;
                f_tw <= 1'b0;
            end else if (ph_exec) begin
                f_idle <= 1'b0;
                f_tw <= wait_t;
                f_tmo <= (t_mul == 10'd0) ? 10'd1 : t_mul;
            end else if (f_idle) begin
                f_left <= f_left - 10'd1;
            end else if (f_tw) begin
                f_tmo <= f_tmo - 10'd1;
            end
        end
        if (!rst_n | reset)
            f_mul <= 5'd1;
        else if (ph_exec & set & instr[7:5] == 3'd4)
            f_mul <= (instr[4:0] == 5'd0) ? 5'd1 : instr[4:0];
    end

    always @(*) begin
        if (f_past) begin
            // 1. no stall where ISA.md has none
            if (ph_exec & never)
                assert (done);
            // 2. the delay, exactly
            if (f_idle & stb)
                assert (f_left == 10'd0 ? ph_exec : ph_delay);
            // 3. a stall ends on the first strobe that may end it
            if ((ph_exec | ph_stall) & pull_b)
                assert (done == ~tx_empty);
            if ((ph_exec | ph_stall) & push_b)
                assert (done == ~rx_full);
            if (ph_exec & irq_w)
                assert (!done);
            if (ph_stall & irq_w)
                assert (done == ~flag);
            // 4. a timed WAIT ends by its timeout
            if (f_tw & ph_stall & f_tmo == 10'd1)
                assert (done);
            // the induction's invariants: the side-set as the host may set it, and the
            // checker's counts are the RTL's
            assert (ss_count == 3'd0 | {1'b0, ss_count} + {3'd0, ss_opt} <= 4'd5);
            assert (f_mul == dlymul & f_mul != 5'd0);
            if (f_idle)
                assert (!stalled & (f_left == 10'd0 ? !delaying : delaying & cnt == f_left));
            if (f_tw)
                assert (stalled & !retry & !delaying & wait_t & f_tmo == (cnt == 10'd0 ? 10'd1 : cnt));
        end
    end

endmodule

// The SM with every input free, but for what the chip guarantees: the first clock is a chip
// reset, instr is the IMEM word at PC (it changes only with PC, or across a reset or a host
// write of PC), and the host configures a side-set of at most 5 bits (HOST.md).
module timing (
    input wire        clk,
    input wire        rst_n,
    input wire        en,
    input wire        run,
    input wire        reset,
    input wire [15:0] instr,
    input wire        h_we,
    input wire [5:0]  h_addr,
    input wire [15:0] h_wdata,
    input wire [15:0] gpio_in,
    input wire [15:0] latch_out,
    input wire [15:0] latch_dir,
    input wire        tx_empty,
    input wire [15:0] tx_data,
    input wire        rx_full,
    input wire        tx_fifo_empty,
    input wire        rx_fifo_full,
    input wire [7:0]  peer_irq,
    input wire        in_we,
    input wire [2:0]  in_idx,
    input wire        in_val
);

    wire [7:0] pc, irq;
    wire [2:0] ss_count;
    wire       ss_opt, ph_exec, ph_stall, ph_delay, done, delaying, stalled, retry;
    wire [9:0] cnt;
    wire [4:0] dlymul, tmo_val;

    openpio_sm u_sm (
        .clk (clk), .rst_n (rst_n), .en (en), .run (run), .reset (reset), .strobe (),
        .imem_addr (), .instr (instr),
        .h_we (h_we), .h_addr (h_addr), .h_wdata (h_wdata), .h_rdata (),
        .gpio_in (gpio_in), .latch_out (latch_out), .latch_dir (latch_dir),
        .out_we (), .out_val (), .dir_we (), .dir_val (), .pau_we (), .pau_val (),
        .tx_empty (tx_empty), .tx_data (tx_data), .tx_pop (),
        .rx_full (rx_full), .rx_push (), .rx_data (),
        .tx_fifo_empty (tx_fifo_empty), .rx_fifo_full (rx_fifo_full),
        .irq (irq), .peer_irq (peer_irq), .peer_we (), .peer_idx (), .peer_val (),
        .in_we (in_we), .in_idx (in_idx), .in_val (in_val),
        .irq_mask (), .pc (pc), .stalled (stalled), .fifo_split (),
        // exposed by timing.sby
        .ss_count (ss_count), .ss_opt (ss_opt), .tmo_val (tmo_val), .ph_exec (ph_exec),
        .ph_stall (ph_stall), .ph_delay (ph_delay), .done (done), .delaying (delaying),
        .retry (retry), .cnt (cnt), .dlymul (dlymul)
    );

    timing_check u_check (
        .clk (clk), .rst_n (rst_n), .reset (reset), .en (en), .h_we (h_we), .h_addr (h_addr),
        .instr (instr), .ss_count (ss_count), .ss_opt (ss_opt), .tx_empty (tx_empty),
        .rx_full (rx_full), .irq (irq), .peer_irq (peer_irq), .tmo_val (tmo_val),
        .ph_exec (ph_exec), .ph_stall (ph_stall), .ph_delay (ph_delay), .done (done),
        .delaying (delaying), .stalled (stalled), .retry (retry), .cnt (cnt), .dlymul (dlymul)
    );

    reg        init = 1'b1;
    reg  [7:0] pc_was = 8'd0;
    reg [15:0] instr_was = 16'd0;
    reg        kept = 1'b0;           // the last clock neither reset the SM nor wrote its PC
    always @(posedge clk) begin
        init <= 1'b0;
        pc_was <= pc;
        instr_was <= instr;
        kept <= rst_n & ~reset & ~(h_we & ~en & h_addr == 6'h11);
    end
    always @(*) begin
        if (init)
            assume (!rst_n);
        if (!init & kept & pc == pc_was)
            assume (instr == instr_was);
        if (h_we & h_addr == 6'h02)
            assume (h_wdata[2:0] == 3'd0 | {1'b0, h_wdata[2:0]} + {3'd0, h_wdata[3]} <= 4'd5);
    end

endmodule

`default_nettype wire
