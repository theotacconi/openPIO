// openpio_core.v - openPIO single state machine: control, datapath, IMEM
//
// One clock with en = 1 is exactly one StateMachine.step() of
// sim/model/emu.py. Per clock, in emulator order:
//
//   0. Prescaler   : if clkdiv > 1 or frac != 0, only every Nth clock is a
//                    "strobe"; non-strobe clocks change nothing but the
//                    prescaler itself and prev_gpio_in.
//   1. Delay phase : delay_cnt != 0 -> decrement; PC <= stage_pc on reaching 0.
//   2. Stall phase : stalled -> re-evaluate WAIT / PULL / PUSH / IRQ on the
//                    held instruction word; on release PC (or a delay) advances.
//   3. Execute     : side-set, then the opcode at PC.
//
// Fetch: imem has one cycle of read latency. rd_addr is driven with pc_d (the
// value PC takes at this edge), so rd_data always equals mem[pc] when the core
// next looks at it -- taken JMPs and OUT PC cost no bubble. rd_en drops while
// the next state is stalled or delaying, freezing rd_data so the stall phase
// can re-decode the stalled instruction without a shadow copy of its fields.
//
// Bring-up: hold en = 0 for at least one cycle after the last IMEM write
// (rd_data refreshes from mem[pc] whenever en = 0), then raise en.
//
// Delay: eff_delay = delay * clkdiv (emu.py), up to 31 * 31 = 961 strobes,
// so delay_cnt is 10 bits. A delay that follows a WAIT/PULL/PUSH stall is
// the raw field (not scaled); one that follows an IRQ-wait stall is scaled.
//
// TIMEOUT_VAL: emu.py keeps timeout_val == timeout_counter at all times
// (SET writes both, WAIT issue copies val -> counter, each stall cycle copies
// counter -> val), so a single 5-bit register serves as both.
//
// Not implemented here: the PAU datapaths (pau_cfg bit 0 NRZI / bit 1
// bit-stuffing / bit 3 Manchester TX and the PAU_TX / PAU_BUSY stalls).
// PAU_CFG and PAU_BAUD are stored and exported; with pau_cfg bits 0 and 3
// clear the core matches emu.py exactly.

`default_nettype none

module openpio_core (
    input  wire        clk,
    input  wire        rst_n,            // synchronous, active-low
    input  wire        en,               // 1 = run (one emu step per clock)

    // IMEM programming port
    input  wire        imem_wr_en,
    input  wire [4:0]  imem_wr_addr,
    input  wire [15:0] imem_wr_data,

    // Static configuration (emu.py constructor arguments)
    input  wire [4:0]  cfg_wrap_bottom,
    input  wire [4:0]  cfg_wrap_top,
    input  wire [2:0]  cfg_ss_count,     // side-set bits, 0..5 (count + opt <= 5)
    input  wire        cfg_ss_opt,       // MSB of side-set field is an enable
    input  wire        cfg_ss_pindirs,   // side-set drives pindirs, not gpio_out
    input  wire [2:0]  cfg_ss_base,
    input  wire [2:0]  cfg_in_base,
    input  wire [2:0]  cfg_out_base,
    input  wire [2:0]  cfg_set_base,
    input  wire [2:0]  cfg_set_count,    // 0..5
    input  wire [2:0]  cfg_jmp_pin,
    input  wire [7:0]  cfg_od_mask,      // open-drain pins
    input  wire        cfg_in_shr,       // ISR shift right
    input  wire        cfg_out_shr,      // OSR shift right
    input  wire [4:0]  cfg_clkdiv_int,   // reset value of CLKDIV (0 -> 1)
    input  wire [7:0]  cfg_clkdiv_frac,

    // Pins
    input  wire [7:0]  gpio_in,
    output wire [7:0]  gpio_out,
    output wire [7:0]  gpio_oe,          // pindirs, 1 = drive

    // TX FIFO (host -> SM), first-word-fall-through head
    input  wire        tx_empty,
    input  wire [15:0] tx_data,
    output reg         tx_pop,

    // RX FIFO (SM -> host)
    input  wire        rx_full,
    output wire [15:0] rx_data,
    output reg         rx_push,

    // IRQ flags; *_clr clear flags before this cycle's instruction sees them
    input  wire [7:0]  irq_clr,
    input  wire [7:0]  peer_irq_clr,
    output wire [7:0]  irq_flags,
    output wire [7:0]  peer_irq_flags,

    // PAU configuration registers (written by SET, consumed by a PAU block)
    output wire [4:0]  pau_cfg,
    output wire [4:0]  pau_baud,

    // Debug / verification
    output wire [4:0]  dbg_pc,
    output wire [15:0] dbg_instr,
    output wire [15:0] dbg_x,
    output wire [15:0] dbg_y,
    output wire [15:0] dbg_isr,
    output wire [15:0] dbg_osr,
    output wire [4:0]  dbg_isr_cnt,
    output wire [4:0]  dbg_osr_cnt,
    output wire [9:0]  dbg_delay,
    output wire        dbg_stalled,
    output wire [4:0]  dbg_clkdiv,
    output wire [4:0]  dbg_timeout,
    output wire        dbg_timeout_flag
);

    // ---------------------------------------------------------------
    // Opcodes and sub-field encodings (ISA.md)
    // ---------------------------------------------------------------
    localparam OP_JMP  = 3'b000;
    localparam OP_WAIT = 3'b001;
    localparam OP_IN   = 3'b010;
    localparam OP_OUT  = 3'b011;
    localparam OP_PUSH = 3'b100;
    localparam OP_ALU  = 3'b101;
    localparam OP_IRQ  = 3'b110;
    localparam OP_SET  = 3'b111;

    localparam R_PINS = 3'd0;  // shared IN / OUT / ALU / SET operand codes
    localparam R_X    = 3'd1;
    localparam R_Y    = 3'd2;
    localparam R_NULL = 3'd3;
    localparam R_ISR  = 3'd4;
    localparam R_OSR  = 3'd5;
    localparam R_6    = 3'd6;  // OUT/SET PINDIRS, ALU STATUS
    localparam R_7    = 3'd7;  // OUT PC, SET PAU_CFG

    localparam SET_CLKDIV  = 3'd3;
    localparam SET_TIMEOUT = 3'd4;
    localparam SET_BAUD    = 3'd5;

    // ---------------------------------------------------------------
    // State
    // ---------------------------------------------------------------
    reg  [4:0]  pc, stage_pc;
    reg  [9:0]  delay_cnt;
    reg         stalled;
    reg  [15:0] x, y;
    reg  [7:0]  gpio_out_r, pindirs_r, prev_gpio_in;
    reg  [7:0]  irq_r, peer_irq_r;
    reg  [4:0]  tmo_cnt;
    reg         tmo_flag;
    reg  [4:0]  clkdiv;
    reg  [5:0]  div_cnt, div_period;
    reg  [7:0]  div_acc;
    reg  [4:0]  pau_cfg_r, pau_baud_r;

    // Next-state (combinational)
    reg  [4:0]  pc_d, stage_pc_d;
    reg  [9:0]  delay_d;
    reg         stalled_d;
    reg  [15:0] x_d, y_d;
    reg  [7:0]  gpio_out_d, pindirs_d;
    reg  [7:0]  irq_d, peer_irq_d;
    reg  [4:0]  tmo_d;
    reg         tmo_flag_d;
    reg  [4:0]  pau_cfg_d, pau_baud_d;

    // ---------------------------------------------------------------
    // Instruction memory
    // ---------------------------------------------------------------
    wire [15:0] instr;
    wire        imem_rd_en = ~en | ~(stalled_d | (|delay_d));

    imem u_imem (
        .clk     (clk),
        .rst_n   (rst_n),
        .wr_en   (imem_wr_en),
        .wr_addr (imem_wr_addr),
        .wr_data (imem_wr_data),
        .rd_en   (imem_rd_en),
        .rd_addr (pc_d),
        .rd_data (instr)
    );

    // ---------------------------------------------------------------
    // Decode
    // ---------------------------------------------------------------
    wire [2:0] opcode  = instr[15:13];
    wire [4:0] ds      = instr[12:8];
    wire [2:0] f_hi    = instr[7:5];   // JMP cond / IN src / OUT,ALU,SET dest
    wire [3:0] f_cnt   = instr[3:0];   // IN / OUT bit count (0 = 16)
    wire [1:0] f_aluop = instr[4:3];
    wire [2:0] f_lo    = instr[2:0];   // ALU src / WAIT,IRQ index
    wire [4:0] f_imm   = instr[4:0];   // JMP target / SET data
    wire [1:0] f_wsrc  = instr[4:3];
    wire       f_b7    = instr[7];     // WAIT pol / PULL / IRQ clear
    wire       f_b6    = instr[6];     // WAIT edge / Block / IRQ wait
    wire       f_b5    = instr[5];     // WAIT t_en / IfCond / IRQ peer

    // Delay / side-set split of instr[12:8]
    wire [2:0] ss_total = (cfg_ss_count == 3'd0) ? 3'd0
                                                 : cfg_ss_count + {2'b00, cfg_ss_opt};
    wire [2:0] dly_bits = 3'd5 - ss_total;
    wire [4:0] dly_mask = ~(5'h1F << dly_bits);
    wire [4:0] delay_f  = ds & dly_mask;
    wire [4:0] ss_field = ds >> dly_bits;
    wire [4:0] ss_vmask = ~(5'h1F << cfg_ss_count);
    wire [4:0] ss_en_sh = ss_field >> cfg_ss_count;
    wire       ss_en    = (cfg_ss_count != 3'd0) & (~cfg_ss_opt | ss_en_sh[0]);
    wire [7:0] ss_m_lo  = ~(8'hFF << cfg_ss_count);
    wire [7:0] ss_mask  = ss_m_lo << cfg_ss_base;
    wire [7:0] ss_bits  = {3'b000, ss_field & ss_vmask} << cfg_ss_base;

    // Default successor with wrap
    wire [4:0] dflt_next = (pc == cfg_wrap_top) ? cfg_wrap_bottom : pc + 5'd1;

    // ---------------------------------------------------------------
    // Prescaler
    // ---------------------------------------------------------------
    wire       div_active     = (clkdiv != 5'd1) | (|cfg_clkdiv_frac);
    wire [8:0] div_acc_sum    = {1'b0, div_acc} + {1'b0, cfg_clkdiv_frac};
    wire [5:0] div_period_now = (div_cnt == 6'd0) ? {1'b0, clkdiv} + {5'b0, div_acc_sum[8]}
                                                  : div_period;
    wire [5:0] div_cnt_inc    = div_cnt + 6'd1;
    wire       strobe         = ~div_active | (div_cnt_inc >= div_period_now);

    wire step     = en & strobe;
    wire ph_delay = step & (|delay_cnt);
    wire ph_stall = step & ~(|delay_cnt) & stalled;
    wire ph_exec  = step & ~(|delay_cnt) & ~stalled;

    // CLKDIV written this cycle; emu.py scales the delay with the new value
    wire       clkdiv_wr = ph_exec & (opcode == OP_SET) & (f_hi == SET_CLKDIV);
    wire [4:0] clkdiv_d  = clkdiv_wr ? ((f_imm == 5'd0) ? 5'd1 : f_imm) : clkdiv;
    wire [9:0] eff_delay = delay_f * clkdiv_d;

    // ---------------------------------------------------------------
    // Shifter / ALU
    // ---------------------------------------------------------------
    reg         isr_shift, isr_load, osr_shift, osr_load;
    reg  [15:0] isr_shift_data, isr_load_data, osr_load_data;
    reg  [4:0]  isr_load_cnt;
    wire [15:0] isr, osr, osr_shift_data;
    wire [4:0]  isr_cnt, osr_cnt;
    wire        isr_full, osr_empty;

    shifter u_shifter (
        .clk            (clk),
        .rst_n          (rst_n),
        .cfg_in_shr     (cfg_in_shr),
        .cfg_out_shr    (cfg_out_shr),
        .shift_cnt      (f_cnt),
        .isr_shift      (isr_shift),
        .isr_shift_data (isr_shift_data),
        .isr_load       (isr_load),
        .isr_load_data  (isr_load_data),
        .isr_load_cnt   (isr_load_cnt),
        .isr            (isr),
        .isr_cnt        (isr_cnt),
        .isr_full       (isr_full),
        .osr_shift      (osr_shift),
        .osr_load       (osr_load),
        .osr_load_data  (osr_load_data),
        .osr            (osr),
        .osr_cnt        (osr_cnt),
        .osr_empty      (osr_empty),
        .osr_shift_data (osr_shift_data)
    );

    // ALU operands: source PINS is raw gpio_in, STATUS / reserved read 0
    reg  [15:0] alu_a, alu_b;
    wire [15:0] alu_y;

    always @(*) begin
        case (f_hi)
            R_X:     alu_a = x;
            R_Y:     alu_a = y;
            R_ISR:   alu_a = isr;
            R_OSR:   alu_a = osr;
            default: alu_a = 16'd0;
        endcase
        case (f_lo)
            R_PINS:  alu_b = {8'd0, gpio_in};
            R_X:     alu_b = x;
            R_Y:     alu_b = y;
            R_ISR:   alu_b = isr;
            R_OSR:   alu_b = osr;
            default: alu_b = 16'd0;
        endcase
    end

    alu u_alu (
        .op (f_aluop),
        .a  (alu_a),
        .b  (alu_b),
        .y  (alu_y)
    );

    // IN source: PINS is gpio_in shifted down by in_base
    reg [15:0] in_val;

    always @(*) begin
        case (f_hi)
            R_PINS:  in_val = {8'd0, gpio_in >> cfg_in_base};
            R_X:     in_val = x;
            R_Y:     in_val = y;
            R_ISR:   in_val = isr;
            R_OSR:   in_val = osr;
            default: in_val = 16'd0;
        endcase
    end

    // ---------------------------------------------------------------
    // Condition logic
    // ---------------------------------------------------------------
    // External clears act before this cycle's instruction (emu: host pokes
    // irq_flags between steps).
    wire [7:0] irq_eff      = irq_r & ~irq_clr;
    wire [7:0] peer_irq_eff = peer_irq_r & ~peer_irq_clr;

    // WAIT
    wire [2:0] wait_pin = cfg_in_base + f_lo;
    reg        w_cur, w_prev;

    always @(*) begin
        case (f_wsrc)
            2'd0:    begin w_cur = gpio_in[f_lo];         w_prev = prev_gpio_in[f_lo];     end
            2'd1:    begin w_cur = gpio_in[wait_pin];     w_prev = prev_gpio_in[wait_pin]; end
            2'd2:    begin w_cur = irq_eff[f_lo];         w_prev = w_cur;                  end
            default: begin w_cur = peer_irq_eff[f_lo];    w_prev = w_cur;                  end
        endcase
    end

    wire wait_cond = ~f_b6 ? (w_cur == f_b7)
                           : (f_b7 ? (~w_prev & w_cur) : (w_prev & ~w_cur));

    wire [4:0] tmo_dec    = (tmo_cnt == 5'd0) ? 5'd0 : tmo_cnt - 5'd1;
    wire       irq_sel    = f_b5 ? peer_irq_eff[f_lo] : irq_eff[f_lo];

    // Pin write masks
    wire [7:0] cnt_m8   = ~(8'hFF << {(f_cnt == 4'd0), f_cnt});
    wire [7:0] out_mask = cnt_m8 << cfg_out_base;
    wire [7:0] out_bits = osr_shift_data[7:0] << cfg_out_base;
    wire [7:0] set_m_lo = {3'b000, ~(5'h1F << cfg_set_count)};
    wire [7:0] set_mask = set_m_lo << cfg_set_base;
    wire [7:0] set_bits = {3'b000, f_imm} << cfg_set_base;

    // ---------------------------------------------------------------
    // Next-state logic
    // ---------------------------------------------------------------
    reg        advance;      // leave stall / finish issue: move PC or start delay
    reg        issue_stall;
    reg [9:0]  adv_delay;
    reg [4:0]  adv_pc;
    reg [7:0]  g, p;         // gpio_out / pindirs, updated in emu order

    always @(*) begin
        pc_d       = pc;
        stage_pc_d = stage_pc;
        delay_d    = delay_cnt;
        stalled_d  = stalled;
        x_d        = x;
        y_d        = y;
        irq_d      = irq_eff;
        peer_irq_d = peer_irq_eff;
        tmo_d      = tmo_cnt;
        tmo_flag_d = tmo_flag;
        pau_cfg_d  = pau_cfg_r;
        pau_baud_d = pau_baud_r;
        g          = gpio_out_r;
        p          = pindirs_r;

        isr_shift      = 1'b0;
        isr_shift_data = in_val;
        isr_load       = 1'b0;
        isr_load_data  = 16'd0;
        isr_load_cnt   = 5'd0;
        osr_shift      = 1'b0;
        osr_load       = 1'b0;
        osr_load_data  = tx_data;
        tx_pop         = 1'b0;
        rx_push        = 1'b0;

        advance     = 1'b0;
        issue_stall = 1'b0;
        adv_delay   = eff_delay;
        adv_pc      = dflt_next;

        if (ph_delay) begin
            // -------------------------------------------------- delay
            delay_d = delay_cnt - 10'd1;
            if (delay_cnt == 10'd1)
                pc_d = stage_pc;

        end else if (ph_stall) begin
            // -------------------------------------------------- stall
            adv_delay = (opcode == OP_IRQ) ? eff_delay : {5'd0, delay_f};
            case (opcode)
                OP_WAIT: begin
                    if (f_b5) begin
                        tmo_d = tmo_dec;
                        if (tmo_dec == 5'd0) begin
                            tmo_flag_d = 1'b1;
                            advance    = 1'b1;
                        end
                    end
                    if (wait_cond)
                        advance = 1'b1;
                end
                OP_PUSH: begin
                    if (f_b7) begin
                        if (!tx_empty) begin
                            tx_pop   = 1'b1;
                            osr_load = 1'b1;
                            advance  = 1'b1;
                        end
                    end else if (!rx_full) begin
                        rx_push  = 1'b1;
                        isr_load = 1'b1;
                        advance  = 1'b1;
                    end
                end
                OP_IRQ: begin
                    if (!irq_sel)
                        advance = 1'b1;
                end
                default: ;
            endcase

        end else if (ph_exec) begin
            // -------------------------------------------------- execute
            if (ss_en) begin
                if (cfg_ss_pindirs) begin
                    p = (p & ~ss_mask) | (ss_bits & ss_mask);
                end else begin
                    g = (g & ~ss_mask) | (ss_bits & ss_mask);
                    p = (p & ~cfg_od_mask) | (~g & cfg_od_mask);
                end
            end

            case (opcode)
                OP_JMP: begin
                    case (f_hi)
                        3'd0: adv_pc = f_imm;
                        3'd1: if (x == 16'd0) adv_pc = f_imm;
                        3'd2: begin
                            if (x != 16'd0) adv_pc = f_imm;
                            x_d = x - 16'd1;
                        end
                        3'd3: if (y == 16'd0) adv_pc = f_imm;
                        3'd4: begin
                            if (y != 16'd0) adv_pc = f_imm;
                            y_d = y - 16'd1;
                        end
                        3'd5: if (tmo_flag)              adv_pc = f_imm;
                        3'd6: if (gpio_in[cfg_jmp_pin])  adv_pc = f_imm;
                        default: if (!osr_empty)         adv_pc = f_imm;
                    endcase
                end

                OP_WAIT: begin
                    if (!wait_cond) begin
                        issue_stall = 1'b1;
                        if (f_b5)
                            tmo_flag_d = 1'b0;   // counter <= val is a no-op
                    end
                end

                OP_IN: begin
                    isr_shift = 1'b1;
                end

                OP_OUT: begin
                    osr_shift = 1'b1;
                    case (f_hi)
                        R_PINS: begin
                            g = (g & ~out_mask) | (out_bits & out_mask);
                            p = (p & ~cfg_od_mask) | (~g & cfg_od_mask);
                        end
                        R_X:   x_d = osr_shift_data;
                        R_Y:   y_d = osr_shift_data;
                        R_ISR: begin
                            isr_load      = 1'b1;
                            isr_load_data = osr_shift_data;
                            isr_load_cnt  = {(f_cnt == 4'd0), f_cnt};
                        end
                        R_6:   p = (p & ~out_mask) | (out_bits & out_mask);
                        R_7:   adv_pc = osr_shift_data[4:0];
                        default: ;   // NULL, reserved
                    endcase
                end

                OP_PUSH: begin
                    if (f_b7) begin
                        // PULL
                        if (f_b5 && !osr_empty) begin
                            // IfEmpty not met: no-op
                        end else if (!tx_empty) begin
                            tx_pop   = 1'b1;
                            osr_load = 1'b1;
                        end else if (!f_b6) begin
                            osr_load      = 1'b1;
                            osr_load_data = x;
                        end else begin
                            issue_stall = 1'b1;
                        end
                    end else begin
                        // PUSH
                        if (f_b5 && !isr_full) begin
                            // IfFull not met: no-op
                        end else if (!rx_full) begin
                            rx_push  = 1'b1;
                            isr_load = 1'b1;
                        end else if (!f_b6) begin
                            isr_load = 1'b1;     // drop
                        end else begin
                            issue_stall = 1'b1;
                        end
                    end
                end

                OP_ALU: begin
                    case (f_hi)
                        R_X:   x_d = alu_y;
                        R_Y:   y_d = alu_y;
                        R_ISR: begin
                            isr_load      = 1'b1;
                            isr_load_data = alu_y;
                            isr_load_cnt  = 5'd16;
                        end
                        R_OSR: begin
                            osr_load      = 1'b1;
                            osr_load_data = alu_y;
                        end
                        default: ;
                    endcase
                end

                OP_IRQ: begin
                    if (f_b7) begin
                        if (f_b5) peer_irq_d[f_lo] = 1'b0;
                        else      irq_d[f_lo]      = 1'b0;
                    end else begin
                        if (f_b5) peer_irq_d[f_lo] = 1'b1;
                        else      irq_d[f_lo]      = 1'b1;
                        if (f_b6)
                            issue_stall = 1'b1;
                    end
                end

                default: begin // OP_SET
                    case (f_hi)
                        R_PINS: begin
                            g = (g & ~set_mask) | (set_bits & set_mask);
                            p = (p & ~cfg_od_mask) | (~g & cfg_od_mask);
                        end
                        R_X:         x_d = {11'd0, f_imm};
                        R_Y:         y_d = {11'd0, f_imm};
                        SET_CLKDIV:  ;   // see clkdiv_d
                        SET_TIMEOUT: begin
                            tmo_d      = f_imm;
                            tmo_flag_d = 1'b0;
                        end
                        SET_BAUD:    pau_baud_d = f_imm;
                        R_6:         p = (p & ~set_mask) | (set_bits & set_mask);
                        default:     pau_cfg_d = f_imm;
                    endcase
                end
            endcase

            stalled_d = issue_stall;
            advance   = ~issue_stall;
        end

        if (advance) begin
            stalled_d = 1'b0;
            if (adv_delay != 10'd0) begin
                delay_d    = adv_delay;
                stage_pc_d = adv_pc;
            end else begin
                pc_d = adv_pc;
            end
        end

        gpio_out_d = g;
        pindirs_d  = p;
    end

    // ---------------------------------------------------------------
    // Registers
    // ---------------------------------------------------------------
    always @(posedge clk) begin
        if (!rst_n) begin
            pc           <= 5'd0;
            stage_pc     <= 5'd0;
            delay_cnt    <= 10'd0;
            stalled      <= 1'b0;
            x            <= 16'd0;
            y            <= 16'd0;
            gpio_out_r   <= 8'd0;
            pindirs_r    <= 8'd0;
            prev_gpio_in <= 8'd0;
            irq_r        <= 8'd0;
            peer_irq_r   <= 8'd0;
            tmo_cnt      <= 5'd0;
            tmo_flag     <= 1'b0;
            clkdiv       <= (cfg_clkdiv_int == 5'd0) ? 5'd1 : cfg_clkdiv_int;
            div_cnt      <= 6'd0;
            div_period   <= 6'd0;
            div_acc      <= 8'd0;
            pau_cfg_r    <= 5'd0;
            pau_baud_r   <= 5'd0;
        end else begin
            pc         <= pc_d;
            stage_pc   <= stage_pc_d;
            delay_cnt  <= delay_d;
            stalled    <= stalled_d;
            x          <= x_d;
            y          <= y_d;
            gpio_out_r <= gpio_out_d;
            pindirs_r  <= pindirs_d;
            irq_r      <= irq_d;
            peer_irq_r <= peer_irq_d;
            tmo_cnt    <= tmo_d;
            tmo_flag   <= tmo_flag_d;
            clkdiv     <= clkdiv_d;
            pau_cfg_r  <= pau_cfg_d;
            pau_baud_r <= pau_baud_d;

            if (en)
                prev_gpio_in <= gpio_in;

            if (en & div_active) begin
                if (div_cnt == 6'd0) begin
                    div_acc    <= div_acc_sum[7:0];
                    div_period <= div_period_now;
                end
                div_cnt <= strobe ? 6'd0 : div_cnt_inc;
            end
        end
    end

    // ---------------------------------------------------------------
    // Outputs
    // ---------------------------------------------------------------
    assign gpio_out         = gpio_out_r;
    assign gpio_oe          = pindirs_r;
    assign rx_data          = isr;
    assign irq_flags        = irq_r;
    assign peer_irq_flags   = peer_irq_r;
    assign pau_cfg          = pau_cfg_r;
    assign pau_baud         = pau_baud_r;

    assign dbg_pc           = pc;
    assign dbg_instr        = instr;
    assign dbg_x            = x;
    assign dbg_y            = y;
    assign dbg_isr          = isr;
    assign dbg_osr          = osr;
    assign dbg_isr_cnt      = isr_cnt;
    assign dbg_osr_cnt      = osr_cnt;
    assign dbg_delay        = delay_cnt;
    assign dbg_stalled      = stalled;
    assign dbg_clkdiv       = clkdiv;
    assign dbg_timeout      = tmo_cnt;
    assign dbg_timeout_flag = tmo_flag;

endmodule

`default_nettype wire
