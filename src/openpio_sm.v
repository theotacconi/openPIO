// openpio_sm.v - one openPIO state machine, ISA v3 (sim/model/ISA.md, emu.py): prescaler,
// fetch, execute, conditions, its PAU and its host registers (HOST.md 0x01..0x1F, 0x30..0x33).
//
// A clock with run = 1 is one emu.py StateMachine.step(). On a strobe, in ISA.md order:
//   PAU step (TX, then RX)  ->  the four conditions it sees (cond_seen)
//   -> delay | stall | execute (side-set, then the instruction)  ->  cond_prev = cond_seen.
// Host register writes land at the end of the clock, over the SM's own (the host wins); reads
// take the clock-start state. CTRL.RESET (reset) lands at the end of the clock too.
//
// Fetch: the IMEM reads imem_addr at the clock edge (one clock of latency, SRAM): imem_addr is
// the next PC, so instr is always mem[pc] (taken jumps and OUT PC cost nothing). The host
// leaves a clock between its last IMEM write and EN (ISA.md, Constraints).
//
// Stalls: the stalled instruction is still on instr. RETRY (an instruction that found no room,
// no bits or a non-empty TX queue) issues again on the next strobe; the others are decoded from
// the word and re-evaluated. Delays, WAIT timeouts and CRC feed bits share one counter: a
// delay starts only when its instruction's stall ends.

`default_nettype none

module openpio_sm (
    input  wire        clk,
    input  wire        rst_n,         // synchronous, active-low (chip reset)
    input  wire        en,            // CTRL EN: RW* registers are writable while 0
    input  wire        run,           // EN or STEP: the prescaler runs, strobes execute
    input  wire        reset,         // CTRL.RESET, at the end of this clock
    output wire        strobe,        // this clock is a strobe of the running SM

    output wire [7:0]  imem_addr,     // read at the clock edge
    input  wire [15:0] instr,         // mem[pc]

    input  wire        h_we,          // host register write, at the end of the clock
    input  wire [5:0]  h_addr,        // 0x00..0x1F, 0x30..0x3F
    input  wire [15:0] h_wdata,
    output reg  [15:0] h_rdata,       // register h_addr (0x00 STATUS and 0x1C FIFO_LEVEL: 0)

    input  wire [15:0] gpio_in,       // the pins through the synchronizer
    input  wire [15:0] latch_out,     // the shared latches, as at the clock start
    input  wire [15:0] latch_dir,
    output wire [15:0] out_we,        // this clock's writes to them (PIN_MASK applied)
    output wire [15:0] out_val,
    output wire [15:0] dir_we,
    output wire [15:0] dir_val,
    output wire [15:0] pau_we,        // pins the PAU TX drives over the latch, from flip-flops
    output wire [15:0] pau_val,

    input  wire        tx_empty,      // TX FIFO front
    input  wire [15:0] tx_data,
    output reg         tx_pop,
    input  wire        rx_full,       // RX FIFO front
    output reg         rx_push,
    output reg  [15:0] rx_data,
    input  wire        tx_fifo_empty, // the whole FIFOs: SM status 6, 7
    input  wire        rx_fifo_full,

    output reg  [7:0]  irq,           // IRQ flags
    input  wire [7:0]  peer_irq,      // the peer's, as at the clock start
    output reg         peer_we,       // IRQ peer: sets / clears the peer's flag peer_idx
    output wire [2:0]  peer_idx,
    output wire        peer_val,
    input  wire        in_we,         // the peer's write to our flag in_idx: lands after ours
    input  wire [2:0]  in_idx,
    input  wire        in_val,

    output reg  [9:0]  irq_mask,      // IRQ_MASK
    output reg  [7:0]  pc,
    output reg         stalled
);

    // ================================================================== host configuration
    reg  [7:0]  wrap_bot, wrap_top;
    reg  [2:0]  ss_count;
    reg         ss_opt, ss_pdir;
    reg  [3:0]  ss_base, in_base, out_base, set_base;
    reg  [2:0]  set_count;
    reg  [15:0] pin_mask;
    reg         in_shr, out_shr;
    reg  [3:0]  push_thr, pull_thr;
    reg  [15:0] clkdiv_cfg;
    reg  [7:0]  frac_cfg;
    reg  [31:0] cond_cfg;
    reg  [1:0]  p_pin2;               // PAU_MODE fields
    reg         p_inv, p_align, p_ext, p_cpol, p_cpha, p_sequal;
    reg  [3:0]  p_slen;
    reg  [15:0] p_code;               // PAU_CODE
    reg  [4:0]  p_baud_rst;           // PAU_CFG fields
    reg  [1:0]  p_run;
    reg  [6:0]  crc_cfg;
    reg  [31:0] crc_poly, crc_res;

    // ================================================================== state
    reg  [7:0]  stage;                // PC after the delay
    reg  [15:0] x, y, isr, osr;
    reg  [4:0]  isr_cnt, osr_cnt;
    reg  [9:0]  cnt;                  // delay, WAIT timeout or CRC feed bits left
    reg         delaying, retry;
    reg  [4:0]  dlymul, tmo_val;
    reg         tmo_flag;
    reg  [31:0] cond;                 // COND, slots 3..0
    reg  [3:0]  cond_prev;            // what the last strobe saw
    reg         fresh;                // no strobe since RESET: no WAIT edge
    reg  [15:0] prev_gpio, time_r;
    reg  [15:0] clkdiv;               // prescaler: CLKDIV and CLKDIV_FRAC since RESET
    reg  [7:0]  frac, div_acc;
    reg  [15:0] div_cnt;
    reg         div_carry;            // the period is CLKDIV + this carry
    reg         strobe_r;             // the next running clock is a strobe (one clock ahead)

    // ================================================================== prescaler
    // emu.py: when CLKDIV > 1 or a fraction, a period starts (count 0) with the accumulator
    // adding the fraction; a strobe ends it. strobe_r applies that rule to the next state, so
    // the add and compare stay off the execute path.
    wire        div_on   = (clkdiv > 16'd1) | (frac != 8'd0);
    wire [8:0]  acc_sum  = div_acc + frac;
    wire        div_load = div_cnt == 16'd0;
    wire [7:0]  acc_n    = div_load ? acc_sum[7:0] : div_acc;
    wire        carry    = div_load ? acc_sum[8] : div_carry;
    wire [16:0] per_n    = {1'b0, clkdiv} + {16'd0, carry};
    wire [15:0] cnt_n    = strobe_r ? 16'd0 : div_cnt + 16'd1;
    wire        carry_n  = acc_n > ~frac;                  // the carry of acc_n + frac
    wire [16:0] per_nn   = (cnt_n == 16'd0) ? {1'b0, clkdiv} + {16'd0, carry_n} : per_n;
    wire        strobe_n = ~div_on | ({1'b0, cnt_n} + 17'd1 >= per_nn);

    wire stb = run & strobe_r;
    assign strobe = stb;

    // ================================================================== decode
    wire [2:0] op      = instr[15:13];
    wire [4:0] ds      = instr[12:8];
    wire [2:0] f_hi    = instr[7:5];          // JMP cond, IN src, OUT / ALU / SET dest
    wire       f_auto  = instr[4];
    wire [4:0] n       = {instr[3:0] == 4'd0, instr[3:0]};   // IN / OUT / feed count, 1..16
    wire [2:0] alu_op  = {instr[13], instr[4:3]};
    wire [2:0] alu_src = instr[2:0];
    wire [1:0] c_type  = instr[7:6];
    wire [5:0] target  = {instr[13], instr[4:0]};          // in the page; FAR: page, entry
    wire [7:0] near    = {pc[7:6], target};
    wire [7:0] far     = {target[5:4], 2'd0, target[3:0]};
    wire [4:0] imm     = instr[4:0];

    localparam OP_IN = 3'b010, OP_OUT = 3'b011, OP_CTRL = 3'b110, OP_SET = 3'b111;
    localparam R_PINS = 3'd0, R_PINDIRS = 3'd1, R_X = 3'd2, R_Y = 3'd3, R_ISR = 3'd4,
               R_OSR = 3'd5, R_SPECIAL = 3'd7;      // 3'd6: NULL

    wire is_jmp  = op[2:1] == 2'b00;
    wire is_alu  = op[2:1] == 2'b10;
    wire is_irq  = op == OP_CTRL & c_type == 2'd0;
    wire is_fifo = op == OP_CTRL & c_type == 2'd1 & instr[2:0] == 3'd0;
    wire is_wait = op == OP_CTRL & c_type == 2'd2 & ~instr[2];
    wire is_pau  = op == OP_CTRL & c_type == 2'd3 & (instr[5:4] == 2'd3 | instr[3:0] == 4'd0);
    wire is_feed = is_pau & instr[5:4] == 2'd3;

    // delay / side-set field
    wire [2:0] ss_tot   = (ss_count == 3'd0) ? 3'd0 : ss_count + {2'd0, ss_opt};
    wire [2:0] dbits    = 3'd5 - ss_tot;
    wire [4:0] delay_f  = ds & ~(5'h1F << dbits);
    wire [4:0] ss_field = ds >> dbits;
    wire       ss_on    = (ss_count != 3'd0) & (~ss_opt | |(ss_field & (5'd1 << ss_count)));
    // pin groups from a base stop at pin 15 (ISA.md, Multiple state machines)
    wire [15:0] ss_m    = {11'd0, ~(5'h1F << ss_count)} << ss_base;
    wire [15:0] ss_v    = ({11'd0, ss_field} << ss_base) & ss_m;

    wire [7:0] pc_next  = (pc == wrap_top) ? wrap_bot : pc + 8'd1;
    wire [4:0] pull_at  = {pull_thr == 4'd0, pull_thr};
    wire [4:0] push_at  = {push_thr == 4'd0, push_thr};
    wire [15:0] m_n     = ~(16'hFFFF << n);                 // the low n bits

    // ================================================================== PAU
    reg        p_ctl_we, p_baud_we, p_tx_push, p_rx_take, p_crc_reset, p_append, p_abort, p_feed;
    reg  [4:0] p_baud;
    reg        p_feed_bit;
    wire       pau_tx, pau_rx;    // PAU_CTL TX, RX on
    wire [12:0] pau_status;           // after the PAU step
    wire [7:0]  pau_status_r;         // at the clock start (host reads)
    wire [4:0] tx_len_s, rx_len_s;
    wire       crc_busy;
    wire [15:0] rx_bits_s;
    wire       pau_drive, pau_line, pau_clk;
    wire [31:0] crc;

    // ================================================================== conditions
    wire [15:0] sm_status = {7'd0, gpio_in != prev_gpio, rx_fifo_full, tx_fifo_empty,
                             isr_cnt >= push_at, osr_cnt >= pull_at, tmo_flag,
                             x == y, y == 16'd0, x == 16'd0};
    wire [15:0] status_w  = {pau_status[7:0], sm_status[7:0]};
    // sources 0 and 1 (pin Index, pin in_base + Index) share one pin multiplexer a slot
    wire [127:0] sig      = {3'd0, pau_status, sm_status, y, x, peer_irq, peer_irq, irq, irq, 32'd0};
    wire [3:0] cond_seen;
    genvar k;
    generate
        for (k = 0; k < 4; k = k + 1) begin : g_cond
            wire [3:0] pin = cond[8*k +: 4] + (cond[8*k + 4] ? in_base : 4'd0);
            wire       val = cond[8*k + 6:8*k + 5] == 2'd0 ? gpio_in[pin] : sig[cond[8*k +: 7]];
            assign cond_seen[k] = val ^ cond[8*k + 7];
        end
    endgenerate


    // ================================================================== operands
    wire [15:0] out_view = ss_on & ~ss_pdir ? (latch_out & ~(ss_m & pin_mask)) | (ss_v & pin_mask) : latch_out;
    wire [15:0] dir_view = ss_on &  ss_pdir ? (latch_dir & ~(ss_m & pin_mask)) | (ss_v & pin_mask) : latch_dir;

    function [15:0] rev16;
        input [15:0] v;
        integer j;
        for (j = 0; j < 16; j = j + 1) rev16[j] = v[15 - j];
    endfunction

    function [15:0] operand;              // IN / ALU source (IN: PINS and PINDIRS below)
        input [2:0] code;
        case (code)
            R_PINS:    operand = gpio_in;
            R_PINDIRS: operand = dir_view;
            R_X:       operand = x;
            R_Y:       operand = y;
            R_ISR:     operand = isr;
            R_OSR:     operand = osr;
            R_SPECIAL: operand = status_w;
            default:   operand = 16'd0;
        endcase
    endfunction

    // IN: the source's low n bits; through the PAU, the oldest n queued bits (oldest first in
    // time order: the LSB shifting right, the MSB shifting left)
    wire        in_pau  = f_hi == R_PINS & pau_rx;
    wire [15:0] in_src  = in_pau ? (in_shr ? rx_bits_s : rev16(rx_bits_s) >> (5'd16 - n))
                        : f_hi == R_PINS    ? gpio_in >> in_base
                        : f_hi == R_PINDIRS ? time_r
                        : operand(f_hi);
    wire [15:0] in_v    = in_src & m_n;
    wire [15:0] isr_in  = in_shr ? (isr >> n) | (in_v << (5'd16 - n)) : (isr << n) | in_v;
    wire [5:0]  isr_sum = isr_cnt + n;
    wire [4:0]  isr_cnt_in = isr_sum > 6'd16 ? 5'd16 : isr_sum[4:0];

    // OUT: the n bits shifted out; the PAU queues them in time order
    wire [15:0] out_v   = out_shr ? osr & m_n : (osr >> (5'd16 - n)) & m_n;
    wire [15:0] osr_out = out_shr ? osr >> n : osr << n;
    wire [5:0]  osr_sum = osr_cnt + n;
    wire [4:0]  osr_cnt_out = osr_sum > 6'd16 ? 5'd16 : osr_sum[4:0];
    wire [15:0] tx_bits = out_shr ? out_v : rev16(osr) & m_n;

    // ALU
    reg  [15:0] alu_d, alu_r;
    wire [15:0] alu_s = operand(alu_src);
    always @(*) begin
        case (f_hi)
            R_PINS:    alu_d = out_view;
            R_PINDIRS: alu_d = dir_view;
            R_X:       alu_d = x;
            R_Y:       alu_d = y;
            R_ISR:     alu_d = isr;
            R_OSR:     alu_d = osr;
            default:   alu_d = 16'd0;
        endcase
        case (alu_op)
            3'd0:    alu_r = alu_s;
            3'd1:    alu_r = rev16(alu_s);
            3'd2:    alu_r = alu_d & alu_s;
            3'd3:    alu_r = alu_d | alu_s;
            3'd4:    alu_r = alu_d ^ alu_s;
            3'd5:    alu_r = ~(alu_d ^ alu_s);
            3'd6:    alu_r = alu_d + alu_s;
            default: alu_r = alu_d - alu_s;
        endcase
    end

    wire [15:0] out_m = m_n << out_base;
    wire [15:0] set_m = {11'd0, ~(5'h1F << set_count)} << set_base;

    // CRC feed: bit r of the last n, r = bits left (n at issue): oldest first
    wire [3:0]  feed_r   = (stalled & ~retry) ? cnt[3:0] : instr[3:0];   // 0: 16
    wire [3:0]  feed_idx = in_shr ? 4'd0 - feed_r : feed_r - 4'd1;

    wire        ph_delay = stb & delaying;
    wire        ph_stall = stb & ~delaying & stalled & ~retry;
    wire        ph_exec  = stb & ~delaying & (~stalled | retry);
    wire        met      = instr[4] ? ~fresh & (cond_prev[instr[1:0]] == ~instr[5]) & (cond_seen[instr[1:0]] == instr[5])
                                    : cond_seen[instr[1:0]] == instr[5];   // WAIT: edge / level

    // delay_f or TIMEOUT_VAL (a timed WAIT that stalls at issue), times DLYMUL
    wire [4:0] mul_a = (ph_exec & is_wait & instr[3] & ~met) ? tmo_val : delay_f;
    wire [9:0] dly_x = mul_a * dlymul;

    // ================================================================== next state
    reg  [7:0]  pc_d, stage_d;
    reg  [15:0] x_d, y_d, isr_d, osr_d;
    reg  [4:0]  isr_cnt_d, osr_cnt_d;
    reg  [9:0]  cnt_d;
    reg         delaying_d, stalled_d, retry_d, tmo_flag_d;
    reg  [4:0]  dlymul_d, tmo_val_d;
    reg  [7:0]  irq_d;
    reg  [31:0] cond_d;
    reg  [15:0] wo_m, wo_v, wd_m, wd_v;    // the instruction's pin writes
    reg         done;                       // the instruction completes: PC or a delay
    reg  [7:0]  done_pc;

    wire        irq_sel  = instr[3] ? peer_irq[instr[2:0]] : irq[instr[2:0]];

    assign peer_idx = instr[2:0];
    assign peer_val = ~instr[5];

    always @(*) begin
        pc_d = pc;  stage_d = stage;  x_d = x;  y_d = y;  isr_d = isr;  osr_d = osr;
        isr_cnt_d = isr_cnt;  osr_cnt_d = osr_cnt;  cnt_d = cnt;
        delaying_d = delaying;  stalled_d = stalled;  retry_d = 1'b0;  tmo_flag_d = tmo_flag;
        dlymul_d = dlymul;  tmo_val_d = tmo_val;  irq_d = irq;  cond_d = cond;
        {wo_m, wo_v, wd_m, wd_v} = 64'd0;
        tx_pop = 1'b0;  rx_push = 1'b0;  rx_data = isr;  peer_we = 1'b0;
        {p_ctl_we, p_baud_we, p_tx_push, p_rx_take, p_crc_reset, p_append, p_abort, p_feed} = 8'd0;
        p_baud = imm;
        p_feed_bit = isr[feed_idx];
        done = 1'b0;
        done_pc = pc_next;

        if (ph_delay) begin
            cnt_d = cnt - 10'd1;
            if (cnt == 10'd1) begin
                pc_d = stage;
                delaying_d = 1'b0;
            end

        end else if (ph_stall) begin
            if (is_wait) begin
                if (instr[3]) begin                             // timed
                    if (cnt != 10'd0)
                        cnt_d = cnt - 10'd1;
                    if (cnt <= 10'd1) begin
                        tmo_flag_d = 1'b1;
                        done = 1'b1;
                    end
                end
                if (met)
                    done = 1'b1;
            end else if (is_fifo & instr[5]) begin              // PULL
                if (~tx_empty) begin
                    tx_pop = 1'b1;
                    osr_d = tx_data;
                    osr_cnt_d = 5'd0;
                    done = 1'b1;
                end
            end else if (is_irq) begin
                done = ~irq_sel;
            end else if (is_feed) begin
                p_feed = 1'b1;
                cnt_d = cnt - 10'd1;
                done = cnt == 10'd1;
            end else if (~rx_full) begin                        // PUSH, IN autopush
                rx_push = 1'b1;
                isr_d = 16'd0;
                isr_cnt_d = 5'd0;
                done = 1'b1;
            end

        end else if (ph_exec) begin
            stalled_d = 1'b0;
            done = 1'b1;
            if (is_jmp) begin
                case (f_hi)
                    3'd0: done_pc = near;
                    3'd1: done_pc = far;
                    3'd2: begin
                        if (x != 16'd0) done_pc = near;
                        x_d = x - 16'd1;
                    end
                    3'd3: begin
                        if (y != 16'd0) done_pc = near;
                        y_d = y - 16'd1;
                    end
                    default: if (cond_seen[f_hi[1:0]]) done_pc = near;
                endcase

            end else if (op == OP_IN) begin
                if (in_pau & (rx_len_s < n)) begin
                    retry_d = 1'b1;
                end else begin
                    p_rx_take = in_pau;
                    isr_d = isr_in;
                    isr_cnt_d = isr_cnt_in;
                    if (f_auto & (isr_cnt_in >= push_at)) begin
                        rx_data = isr_in;
                        if (~rx_full) begin
                            rx_push = 1'b1;
                            isr_d = 16'd0;
                            isr_cnt_d = 5'd0;
                        end else begin
                            stalled_d = 1'b1;                   // PUSH stall
                            done = 1'b0;
                        end
                    end
                end

            end else if (op == OP_OUT) begin
                if (f_auto & (osr_cnt >= pull_at)) begin        // autopull, then issue again
                    if (~tx_empty) begin
                        tx_pop = 1'b1;
                        osr_d = tx_data;
                        osr_cnt_d = 5'd0;
                    end
                    retry_d = 1'b1;
                end else if (f_hi == R_PINS & pau_tx & (5'd16 - tx_len_s < n)) begin
                    retry_d = 1'b1;
                end else begin
                    osr_d = osr_out;
                    osr_cnt_d = osr_cnt_out;
                    if (f_auto & (osr_cnt_out >= pull_at) & ~tx_empty) begin
                        tx_pop = 1'b1;
                        osr_d = tx_data;
                        osr_cnt_d = 5'd0;
                    end
                    case (f_hi)
                        R_PINS: if (pau_tx) p_tx_push = 1'b1;
                                else begin wo_m = out_m; wo_v = out_v << out_base; end
                        R_PINDIRS: begin wd_m = out_m; wd_v = out_v << out_base; end
                        R_X:       x_d = out_v;
                        R_Y:       y_d = out_v;
                        R_ISR:     begin isr_d = out_v; isr_cnt_d = n; end
                        R_OSR:     p_baud_we = 1'b1;                // PAU_BAUD
                        R_SPECIAL: done_pc = out_v[7:0];
                        default:   ;
                    endcase
                end

            end else if (is_alu) begin
                case (f_hi)
                    R_PINS:    begin wo_m = 16'hFFFF; wo_v = alu_r; end
                    R_PINDIRS: begin wd_m = 16'hFFFF; wd_v = alu_r; end
                    R_X:       x_d = alu_r;
                    R_Y:       y_d = alu_r;
                    R_ISR:     begin isr_d = alu_r; isr_cnt_d = 5'd0; end
                    R_OSR:     begin osr_d = alu_r; osr_cnt_d = 5'd0; end
                    R_SPECIAL: cond_d[8*alu_r[9:8] +: 8] = alu_r[7:0];
                    default:   ;
                endcase

            end else if (is_irq) begin
                if (instr[3])
                    peer_we = 1'b1;
                else
                    irq_d[instr[2:0]] = ~instr[5];
                if (instr[4] & ~instr[5]) begin
                    stalled_d = 1'b1;
                    done = 1'b0;
                end

            end else if (is_fifo) begin
                if (instr[5]) begin                             // PULL
                    if (instr[3] & (osr_cnt < pull_at)) begin
                    end else if (~tx_empty) begin
                        tx_pop = 1'b1;
                        osr_d = tx_data;
                        osr_cnt_d = 5'd0;
                    end else if (~instr[4]) begin
                        osr_d = x;
                        osr_cnt_d = 5'd0;
                    end else begin
                        stalled_d = 1'b1;
                        done = 1'b0;
                    end
                end else begin                                  // PUSH
                    if (instr[3] & (isr_cnt < push_at)) begin
                    end else if (~rx_full | ~instr[4]) begin
                        rx_push = ~rx_full;
                        isr_d = 16'd0;
                        isr_cnt_d = 5'd0;
                    end else begin
                        stalled_d = 1'b1;
                        done = 1'b0;
                    end
                end

            end else if (is_wait) begin
                if (instr[3]) begin
                    cnt_d = dly_x;
                    tmo_flag_d = 1'b0;
                end
                if (~met) begin
                    stalled_d = 1'b1;
                    done = 1'b0;
                end

            end else if (is_pau) begin
                case (instr[5:4])
                    // while the CRC is being sent (an append), only TX abort proceeds
                    2'd0: if (crc_busy) retry_d = 1'b1; else p_crc_reset = 1'b1;
                    2'd1: if (crc_busy | (tx_len_s != 5'd0)) retry_d = 1'b1; else p_append = 1'b1;
                    2'd2: p_abort = 1'b1;
                    default: if (crc_busy) retry_d = 1'b1; else begin   // CRC feed, a bit a strobe
                        p_feed = 1'b1;
                        if (n != 5'd1) begin
                            cnt_d = {5'd0, n} - 10'd1;
                            stalled_d = 1'b1;
                            done = 1'b0;
                        end
                    end
                endcase

            end else if (op == OP_SET) begin
                case (f_hi)
                    3'd0: begin wo_m = set_m; wo_v = {11'd0, imm} << set_base; end
                    3'd1: begin wd_m = set_m; wd_v = {11'd0, imm} << set_base; end
                    3'd2: x_d = {11'd0, imm};
                    3'd3: y_d = {11'd0, imm};
                    3'd4: dlymul_d = (imm == 5'd0) ? 5'd1 : imm;
                    3'd5: begin tmo_val_d = imm; tmo_flag_d = 1'b0; end
                    3'd6: p_baud_we = 1'b1;
                    default: p_ctl_we = 1'b1;
                endcase
            end

            if (retry_d) begin
                stalled_d = 1'b1;
                done = 1'b0;
            end
        end

        if (done) begin                                         // complete: PC, or a delay
            stalled_d = 1'b0;
            if (delay_f != 5'd0) begin
                delaying_d = 1'b1;
                cnt_d = dly_x;
                stage_d = done_pc;
            end else begin
                pc_d = done_pc;
            end
        end
        if (p_baud_we & op == OP_OUT)
            p_baud = out_v[4:0];
    end

    // pins: side-set, then the instruction (it wins), within PIN_MASK
    wire        ss_w = ph_exec & ss_on;
    wire [15:0] so_m = ss_w & ~ss_pdir ? ss_m : 16'd0;
    wire [15:0] sd_m = ss_w &  ss_pdir ? ss_m : 16'd0;
    assign out_we  = (so_m | wo_m) & pin_mask;
    assign out_val = (wo_m & wo_v) | (~wo_m & ss_v);
    assign dir_we  = (sd_m | wd_m) & pin_mask;
    assign dir_val = (wd_m & wd_v) | (~wd_m & ss_v);

    // the PAU drives pin out_base, and out_base + 1 with PIN2: the complement, or the bit clock
    wire        pin2_on = (p_pin2 == 2'd1) | (p_pin2 == 2'd2 & ~p_ext);
    wire        pin2_v  = p_pin2[1] ? pau_clk : ~pau_line;
    wire [15:0] pau_m   = (16'd1 << out_base) | (pin2_on ? 16'd1 << (out_base + 4'd1) : 16'd0);
    assign pau_we  = pau_drive ? pau_m & pin_mask : 16'd0;
    assign pau_val = ({15'd0, pau_line} << out_base) | ({15'd0, pin2_v} << (out_base + 4'd1));

    pau u_pau (
        .clk        (clk),
        .rst_n      (rst_n),
        .reset      (reset),
        .stb        (stb),
        .code       (p_code),
        .inv        (p_inv),
        .align      (p_align),
        .ext        (p_ext),
        .cpol       (p_cpol),
        .cpha       (p_cpha),
        .stuff_len  (p_slen),
        .sequal     (p_sequal),
        .baud_rst   (p_baud_rst),
        .run        (p_run),
        .crc_cfg    (crc_cfg),
        .crc_poly   (crc_poly),
        .crc_res    (crc_res),
        .in_base    (in_base),
        .gpio_in    (gpio_in),
        .ctl_we     (p_ctl_we),
        .ctl_val    (imm),
        .pin_now    (out_view[out_base]),
        .baud_we    (p_baud_we),
        .baud_val   (p_baud),
        .tx_push    (p_tx_push),
        .tx_n       (n),
        .tx_bits    (tx_bits),
        .rx_take    (p_rx_take),
        .rx_n       (n),
        .crc_reset  (p_crc_reset),
        .crc_append (p_append),
        .tx_abort   (p_abort),
        .feed       (p_feed),
        .feed_bit   (p_feed_bit),
        .tx_on      (pau_tx),
        .rx_on      (pau_rx),
        .status     (pau_status),
        .status_r   (pau_status_r),
        .tx_len_s   (tx_len_s),
        .rx_len_s   (rx_len_s),
        .crc_busy   (crc_busy),
        .rx_bits_s  (rx_bits_s),
        .drive      (pau_drive),
        .line       (pau_line),
        .bit_clk    (pau_clk),
        .crc        (crc)
    );

    // ================================================================== host registers
    wire stopped = ~en;
    wire hw = h_we;

    always @(*) begin
        case (h_addr)
            6'h01: h_rdata = {wrap_top, wrap_bot};
            6'h02: h_rdata = {4'd0, ss_base, 3'd0, ss_pdir, ss_opt, ss_count};
            6'h03: h_rdata = {1'b0, set_count, set_base, out_base, in_base};
            6'h04: h_rdata = pin_mask;
            6'h05: h_rdata = {pull_thr, push_thr, 6'd0, out_shr, in_shr};
            6'h06: h_rdata = clkdiv_cfg;
            6'h08: h_rdata = cond_cfg[15:0];
            6'h09: h_rdata = cond_cfg[31:16];
            6'h0A: h_rdata = {4'd0, p_slen, p_sequal, p_cpha, p_cpol, p_ext, p_align, p_inv, p_pin2};
            6'h0B: h_rdata = {9'd0, crc_cfg};
            6'h0C: h_rdata = crc_poly[15:0];
            6'h0D: h_rdata = crc_poly[31:16];
            6'h0E: h_rdata = crc_res[15:0];
            6'h0F: h_rdata = crc_res[31:16];
            6'h10: h_rdata = {8'd0, irq};
            6'h11: h_rdata = {8'd0, pc};
            6'h12: h_rdata = x;
            6'h13: h_rdata = y;
            6'h14: h_rdata = isr;
            6'h15: h_rdata = osr;
            6'h16: h_rdata = {3'd0, osr_cnt, 3'd0, isr_cnt};
            6'h17: h_rdata = cond[15:0];
            6'h18: h_rdata = cond[31:16];
            6'h19: h_rdata = crc[15:0];
            6'h1A: h_rdata = crc[31:16];
            6'h1B: h_rdata = {pau_status_r, sm_status[7:0]};
            6'h1D: h_rdata = {6'd0, irq_mask};
            6'h1E: h_rdata = {8'd0, frac_cfg};
            6'h1F: h_rdata = time_r;
            6'h30: h_rdata = p_code;
            6'h31: h_rdata = {9'd0, p_run, p_baud_rst};
            default: h_rdata = 16'd0;
        endcase
    end

    // ================================================================== registers
    // IRQ flags at the clock end: this SM's write, the peer's, then the host's (clear, set)
    wire [7:0] irq_peer = in_we ? (irq_d & ~(8'd1 << in_idx)) | ({7'd0, in_val} << in_idx) : irq_d;
    wire [7:0] irq_host = (hw & h_addr == 6'h10) ? (irq_peer & ~h_wdata[7:0]) | h_wdata[15:8] : irq_peer;
    wire [4:0] hcnt_i = h_wdata[4:0] > 5'd16 ? 5'd16 : h_wdata[4:0];
    wire [4:0] hcnt_o = h_wdata[12:8] > 5'd16 ? 5'd16 : h_wdata[12:8];

    assign imem_addr = (!rst_n | reset) ? 8'd0 : (hw & stopped & h_addr == 6'h11) ? h_wdata[7:0] : pc_d;

    always @(posedge clk) begin
        if (!rst_n) begin
            wrap_bot <= 8'd0;   wrap_top <= 8'd255;
            ss_count <= 3'd0;   ss_opt <= 1'b0;   ss_pdir <= 1'b0;   ss_base <= 4'd0;
            in_base <= 4'd0;    out_base <= 4'd0; set_base <= 4'd0;  set_count <= 3'd5;
            pin_mask <= 16'hFFFF;
            in_shr <= 1'b1;     out_shr <= 1'b1;  push_thr <= 4'd0;  pull_thr <= 4'd0;
            clkdiv_cfg <= 16'd1;  frac_cfg <= 8'd0;
            cond_cfg <= 32'hE400_6160;
            {p_slen, p_sequal, p_cpha, p_cpol, p_ext, p_align, p_inv, p_pin2} <= 12'd0;
            p_code <= 16'h00FA;
            {p_run, p_baud_rst} <= 7'd0;
            crc_cfg <= 7'd0;  crc_poly <= 32'd0;  crc_res <= 32'd0;
            irq_mask <= 10'h0FF;
        end else if (hw) begin
            case (h_addr)
                6'h01: {wrap_top, wrap_bot} <= h_wdata;
                6'h02: {ss_base, ss_pdir, ss_opt, ss_count} <= {h_wdata[11:8], h_wdata[4:0]};
                6'h03: {set_count, set_base, out_base, in_base} <= h_wdata[14:0];
                6'h04: pin_mask <= h_wdata;
                6'h05: {pull_thr, push_thr, out_shr, in_shr} <= {h_wdata[15:8], h_wdata[1:0]};
                6'h06: clkdiv_cfg <= (h_wdata == 16'd0) ? 16'd1 : h_wdata;
                6'h07: cond_cfg[8*h_wdata[9:8] +: 8] <= h_wdata[7:0];
                6'h0A: {p_slen, p_sequal, p_cpha, p_cpol, p_ext, p_align, p_inv, p_pin2} <= h_wdata[11:0];
                6'h0B: crc_cfg <= h_wdata[6:0];
                6'h0C: crc_poly[15:0] <= h_wdata;
                6'h0D: crc_poly[31:16] <= h_wdata;
                6'h0E: crc_res[15:0] <= h_wdata;
                6'h0F: crc_res[31:16] <= h_wdata;
                6'h1D: irq_mask <= h_wdata[9:0];
                6'h1E: frac_cfg <= h_wdata[7:0];
                6'h30: p_code <= h_wdata;
                6'h31: {p_run, p_baud_rst} <= h_wdata[6:0];
                default: ;
            endcase
        end

        if (!rst_n || reset) begin
            pc <= 8'd0;          stage <= 8'd0;
            x <= 16'd0;          y <= 16'd0;          isr <= 16'd0;        osr <= 16'd0;
            isr_cnt <= 5'd0;     osr_cnt <= 5'd16;
            cnt <= 10'd0;        delaying <= 1'b0;    stalled <= 1'b0;     retry <= 1'b0;
            dlymul <= 5'd1;      tmo_val <= 5'd0;     tmo_flag <= 1'b0;
            irq <= 8'd0;
            time_r <= 16'd0;
            div_cnt <= 16'd0;    div_acc <= 8'd0;     div_carry <= 1'b0;
            cond_prev <= 4'd0;
            fresh <= 1'b1;
            if (!rst_n) begin
                cond <= 32'hE400_6160;
                prev_gpio <= 16'd0;
                clkdiv <= 16'd1;   frac <= 8'd0;   strobe_r <= 1'b1;
            end else begin
                cond <= cond_cfg;
                prev_gpio <= gpio_in;
                clkdiv <= clkdiv_cfg;   frac <= frac_cfg;
                strobe_r <= clkdiv_cfg <= 16'd1;
            end
        end else begin
            if (stb) begin
                {pc, stage, x, y, isr, osr} <= {pc_d, stage_d, x_d, y_d, isr_d, osr_d};
                {isr_cnt, osr_cnt, cnt} <= {isr_cnt_d, osr_cnt_d, cnt_d};
                {delaying, stalled, retry, tmo_flag} <= {delaying_d, stalled_d, retry_d, tmo_flag_d};
                {dlymul, tmo_val, cond} <= {dlymul_d, tmo_val_d, cond_d};
                cond_prev <= cond_seen;
                fresh <= 1'b0;
                prev_gpio <= gpio_in;
                time_r <= time_r + 16'd1;
            end
            irq <= irq_host;
            if (run) begin
                div_cnt <= div_on ? cnt_n : div_cnt;
                div_acc <= div_on ? acc_n : div_acc;
                div_carry <= div_on ? carry : div_carry;
                strobe_r <= strobe_n;
            end
            if (hw & stopped) begin
                case (h_addr)
                    6'h11: pc <= h_wdata[7:0];
                    6'h12: x <= h_wdata;
                    6'h13: y <= h_wdata;
                    6'h14: isr <= h_wdata;
                    6'h15: osr <= h_wdata;
                    6'h16: {osr_cnt, isr_cnt} <= {hcnt_o, hcnt_i};
                    default: ;
                endcase
            end
        end
    end

endmodule

`default_nettype wire
