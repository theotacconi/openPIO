# openPIO reference card

Everything needed to program and drive the chip. Exact rules: `sim/model/ISA.md` (instructions,
PAU, timing) and `sim/model/HOST.md` (SPI, registers).

## 1. The machine

```
 host MCU ──SPI──► openPIO ── SM 0 ─┐ peers (SM i and i^1): each can set the other's IRQ flags
                              SM 1 ─┘ and push / pull into the other's FIFOs
                              8 pins uio[7:0], shared by the SMs

 one SM:  TX FIFO ─pull─► OSR ─out─► pins, or PAU TX ─► pins
          RX FIFO ◄─push─ ISR ◄─in── pins, or PAU RX ◄─ pins
          + IMEM 64 x 16 bit, X, Y, 8 IRQ flags, 4 conditions, clock divider
```

- All registers are 16 bits. FIFOs hold 132 words of 16 bits.
- **Strobe**: one every CLKDIV clocks (CLKDIV = int + frac/256, int 1..31). One instruction per
  strobe. A stalled instruction repeats every strobe. `[n]` then idles n x DLYMUL strobes.
- **Program counter**: after the instruction at `wrap_top`, PC = `wrap_bottom` (unless it jumped).

## 2. Chip pins

| Pin | Use |
|---|---|
| `ui_in[0]` / `[1]` / `[2]` | SPI SCLK / MOSI / CS_N (from the host) |
| `ui_in[7:3]` | extra inputs: replace pins 7..3 as inputs where GPIO[15:11] selects them |
| `uo_out[0]` | SPI MISO |
| `uo_out[1]` | HOST_IRQ: high while any SM has a flag set that its IRQ_MASK lets through |
| `uo_out[7:2]` | copy of the output values of pins 7..2 |
| `uio[7:0]` | pins 0..7: the SMs' I/O |

## 3. Host workflow

1. Write the program, configuration included (§10).
2. Load it (what `sim/model/host.py` sends):
   - CTRL = running SMs without this one
   - IMEM_WR burst, then REG_WR burst from 0x01 (WRAP .. CLKDIV)
   - 4 COND_CFG writes, then REG_WR burst from 0x0A (PAU_MODE .. CRC_RES_HI)
   - GPIO |= the program's open-drain pins
   - CTRL RESET bit, then REG_WR burst from 0x11 (PC, X, Y)
3. Start: CTRL = mask of SMs to run (they start on the same clock).
4. Run: TX_PUSH words in, RX_POP words out. HOST_IRQ or IRQ_SUM says which SM has a flag
   set; read and clear it in its IRQ register.

## 4. SPI

- Mode 0, MSB first, SCLK <= clk / 6.
- A transaction is CS_N low, a 16-bit header, n data words (a burst), then CS_N high.

```
header: Op[15:13] | SM[12:11] | 0[10:6] | Addr[5:0]
```

| Op | Command | Per data word |
|---|---|---|
| `000` | NOP | - |
| `010` | REG_WR | register Addr = word; Addr + 1 |
| `011` | REG_RD | MISO = register Addr; Addr + 1 |
| `100` | TX_PUSH | TX FIFO = word (dropped if full) |
| `101` | RX_POP | MISO = RX FIFO word (0 if empty) |
| `110` | IMEM_WR | instruction Addr = word; Addr + 1 (SM stopped only) |
| `111` | IMEM_RD | MISO = instruction Addr; Addr + 1 |

MISO carries the read data during read words. During the header and every other word it
carries the STATUS register of SM `SM`. Chip registers (Addr >= 0x20) ignore `SM`.

## 5. Registers

Per SM (`SM` field). R = read only, W = write only, RW* = written only while the SM is stopped.

| Addr | Name | | Fields (reset) |
|---|---|---|---|
| 00 | STATUS | R | [0] TX empty, [1] TX full, [2] RX empty, [3] RX full, [4] stalled, [5] running, [13:8] PC |
| 01 | WRAP | RW | [5:0] bottom (0), [13:8] top (63) |
| 02 | SIDESET | RW | [2:0] count (0), [3] opt, [4] to pindirs, [10:8] base |
| 03 | PINBASE | RW | [2:0] in_base, [6:4] out_base, [10:8] set_base, [14:12] set_count (5) |
| 04 | PIN_MASK | RW | pins this SM may change (0xFF) |
| 05 | MISC | RW | [0] ISR shifts right (1), [1] OSR shifts right (1), [11:8] push thr., [15:12] pull thr. (0 = 16) |
| 06 | CLKDIV | RW | [4:0] int (1), [15:8] frac/256 (0); applied at RESET |
| 07 | COND_CFG | W | [9:8] slot, [7:0] condition (§7) |
| 08, 09 | COND_CFG01, 23 | R | slots 1:0, 3:2 (0x6160, 0xE400) |
| 0A | PAU_MODE | RW | [1:0] enc, [2] inv, [3] diff, [7:4] stuff len, [9:8] stuff mode, [14:10] baud (§8) |
| 0B | CRC_CFG | RW | [4:0] width - 1, [5] init ones, [6] append complemented |
| 0C, 0D | CRC_POLY | RW | low, high 16 bits (without the x^width term) |
| 0E, 0F | CRC_RES | RW | low, high: CRC of a correct received frame |
| 10 | IRQ | RW | read: flags [7:0]. Write: [7:0] clears those flags, then [15:8] sets those flags |
| 11..15 | PC, X, Y, ISR, OSR | RW* | |
| 16 | COUNTS | RW* | [4:0] isr_count, [12:8] osr_count |
| 17, 18 | COND01, 23 | R | live conditions (a program can change them) |
| 19, 1A | CRC_LO, HI | R | current CRC |
| 1B | SM_STATUS | R | the STATUS operand: SM status [7:0], PAU status [15:8] (§7) |
| 1C | FIFO_LEVEL | R | [7:0] TX words, [15:8] RX words (0..132) |
| 1D | IRQ_MASK | RW | flags that reach HOST_IRQ (0xFF) |

Chip (any `SM`):

| Addr | Name | | Fields |
|---|---|---|---|
| 20 | CTRL | RW | [3:0] EN (SM i runs while bit i = 1). Write only: [7:4] RESET, [11:8] STEP, bit per SM |
| 21 | GPIO | RW | [7:0] open-drain pins, [15:11] `ui_in[7:3]` replaces pins 7..3 as inputs |
| 22 | PINS | R | [7:0] output values, [15:8] output enables |
| 23 | PINS_IN | R | the pins as the SMs see them |
| 24 | IRQ_SUM | R | bit i: SM i has a flag set in its IRQ_MASK |
| 25 | ID | R | 0x2250: ISA 2, 2 SMs, 'P' |

- **A CTRL write sets EN for all SMs**: to reset or step one SM, write the others' EN bits too.
- **RESET**: clears PC, X, Y, ISR, OSR (empty), IRQ flags, FIFOs, PAU, delays and stalls. It
  also sets COND = COND_CFG, DLYMUL = 1, CRC = its initial value, and reloads CLKDIV and the
  PAU baud.
- **RESET keeps**: configuration, IMEM and the pin latches.
- **STEP**: the stopped SM executes one strobe.

## 6. Instructions

Every instruction can end with `side v` (side-set) and `[n]` (delay). Bits [12:8] hold both:
side-set on top (count bits, plus 1 enable bit with opt), delay below. Side-set writes `count`
pins from `sideset_base` (or their directions) before the instruction runs.

| Syntax | Effect |
|---|---|
| `jmp [cond,] target` | PC = target (0..63) if cond. No cond = always. `x--` / `y--`: jumps if != 0, then decrements |
| `nop` | nothing (delay / side-set only) |
| `in src, n [auto]` | shift n (1..16) bits of src into ISR; isr_count += n |
| `out dst, n [auto]` | shift n bits out of OSR into dst; osr_count += n |
| `mov` `rev` `and` `or` `xor` `xnor` `add` `sub` `dst, src` | dst = src, bit-reverse(src), dst & src, dst \| src, ^, ~^, +, - (16 bits, no carry) |
| `not dst` | dst = ~dst |
| `push [noblock] [iffull] [peer]` | RX FIFO = ISR; ISR and isr_count = 0 |
| `pull [noblock] [ifempty] [peer]` | OSR = TX FIFO word; osr_count = 0 |
| `wait pol cond [edge] [timeout]` | stall until cond matches pol |
| `irq [clear] [wait] [peer] n` | set flag n (0..7) |
| `set dst, v` | dst = v (0..31, or a label = its address) |
| `pau action` | PAU action (§8) |

Operands:

| | pins | pindirs | x, y | isr | osr | null | special |
|---|---|---|---|---|---|---|---|
| `in` src | n pins from in_base (PAU RX bits when RX is on) | - | low n bits | low n bits | low n bits | zeros | `status` |
| `out` dst | n pins from out_base (PAU TX queue when TX is on) | n dirs from out_base | = the bits | = the bits, count = n | - | discard | `pc`: jump |
| ALU src | all 8 inputs | 8 dirs | value | value | value | 0 | `status` |
| ALU dst | 8 outputs (in PIN_MASK) | 8 dirs | value | count = 16 | count = 0 | discard | `cond`: slot [9:8] = [7:0] |

- **Shifts**: shift right (reset) moves bits LSB first, out of bit 0 and into bit 15. Shift left
  moves MSB first.
- **Thresholds**: `auto`, `iffull` and `ifempty` use the thresholds (1..16). OSR "empty" means
  osr_count >= pull threshold; after RESET the OSR is empty.
- **`in ... auto`**: if isr_count >= threshold, push (stalls while RX is full).
- **`out ... auto`**: if the OSR is empty, pull first (stalls while TX is empty). After the
  shift, it reloads at once if a word is waiting, so a fed stream has no gaps.
- **`push` / `pull`**: block by default (stall on full / empty). `noblock`: push drops the word,
  pull copies X into OSR. `iffull` / `ifempty`: act only at the threshold. `peer`: push into the
  peer's TX FIFO, pull from the peer's RX FIFO.
- **`wait`**: pol `1`/`high`, `0`/`low`, `rise`, `fall` (the last two are edges). `edge`
  compares with the previous strobe. `timeout` also ends the wait after TIMEOUT_VAL strobes and
  sets the timeout flag.
- **`irq`**: `clear` clears the flag instead of setting it. `wait` stalls until someone clears
  it. `peer` acts on the peer's flag.
- **`set` destinations**: `pins` / `pindirs` (set_count pins from set_base), `x`, `y`
  (zero-extended), `dlymul` (0 = 1), `timeout_val`, `pau_baud` (0 = 32), `pau_ctl` (§8).

Opcodes [15:13]:

| Opcode | Instruction |
|---|---|
| `000` / `001` | JMP (bit 13 = target bit 5) |
| `010` | IN |
| `011` | OUT |
| `100` / `101` | ALU (bit 13 = op bit 2) |
| `110` | IRQ, PUSH / PULL, WAIT, PAU |
| `111` | SET |

Reserved encodings run as NOP.

## 7. Conditions

`jmp` and `wait` test a condition. The assembler puts the conditions a program uses into the 4
hardware slots: at most 4 different conditions per program. `.cond0`..`.cond3` fix a slot.

| Syntax | True when |
|---|---|
| `gpio n` | pin n (0..7) is high |
| `in_base n` | pin in_base + n (mod 8) is high |
| `irq n` / `peer_irq n` | own / peer flag n is set |
| `x n` / `y n` | bit n (0..15) of X / Y is 1 |
| `status name` / `pau name` | the status bit below is 1 |
| `!` before any of these | inverted |
| `!x` `!y` `x==y` `x!=y` `timeout` `osre` `!osre` | X == 0, Y == 0, X == Y, X != Y, timeout flag, OSR empty, not empty |
| `pin` | the GPIO pin the host picks (`jmp_pin`, default 0) |

Note: `!x` alone means X == 0; `!x 3` means bit 3 of X is 0.

| # | `status` name | `pau` name |
|---|---|---|
| 0 | `x_zero` | `tx_idle`: queue empty, last bit sent |
| 1 | `y_zero` | `rx_idle`: no input edge for 2 bit times |
| 2 | `x_eq_y` | `tx_ready`: TX queue has room for 16 bits |
| 3 | `timeout` | `rx_ready`: RX queue not empty |
| 4 | `osr_empty` | `crc_ok`: CRC == CRC_RES |
| 5 | `isr_full` | `crc_zero`: CRC == 0 |
| 6 | `tx_empty` (TX FIFO) | `stuff_err` (sticky) |
| 7 | `rx_full` (RX FIFO) | `overrun` (sticky) |
| 8 | - | `se0`: pins in_base and in_base+1 both low |
| 9 | - | `se1`: both high |
| 10 | - | `bit_err` (sticky) |

- **Condition byte** (COND_CFG, `mov cond`): Inv[7], Src[6:4], Index[3:0]. Src 0..7 =
  gpio, in_base, irq, peer_irq, x, y, status, pau.
- **Reset slots**: x_zero, y_zero, gpio 0, !osr_empty.

## 8. PAU (serial line engine)

```
out pins, n ─► TX queue 32 bits ─► stuffing ─► encoder ─► pin out_base (diff: + out_base+1 inverted)
in pins, n  ◄─ RX queue 32 bits ◄─ destuffing ◄─ decoder ◄─ pin in_base
               CRC (≤ 32 bits) on the data bits
```

Config: `.pau enc=nrz|nrzi|manchester|bmc [inv] [diff] [stuff=1..16 stuff_mode=ones|zeros|equal]
[baud=H]` and `.crc width=1..32 poly=P [init=ones] [xorout=1] [res=R]`.

| enc | During bit b |
|---|---|
| nrz | line = b |
| nrzi | toggles at the bit start if b = 0 |
| manchester | ~b, then b (1 = rising edge mid-bit) |
| bmc | toggles at the bit start, and mid-bit if b = 1 |

Program control:

- **`set pau_baud, H`**: half-bit = H strobes, bit = 2H strobes.
- **`set pau_ctl, tx|rx|stuff|crc|monitor`** (any combination; 0 = all off):
  - `tx`: OUT PINS queues bits and the PAU drives the pin(s). Turning it off: the queue drains,
    then the pins go back to the latch.
  - `rx`: IN PINS takes decoded bits. Turning it on clears the queue and the errors.
  - `stuff`: TX inserts, RX removes a bit after `stuff` qualifying bits. ones: a 0 after N 1s;
    zeros: a 1 after N 0s; equal: the complement after N equal bits. A wrong one sets stuff_err.
  - `crc`: the CRC takes the data bits of TX, or of RX when TX is off.
  - `monitor`: each sent bit is compared with in_base. A mismatch sets bit_err and silences TX
    (bus arbitration on CAN, LIN, 1-Wire).
- **`pau crc_reset`**: CRC = initial value.
- **`pau crc_append`**: queues the CRC (MSB first).
- **`pau tx_abort`**: drops the queue, TX off, pins back to the latch now.
- **`pau crc_feed, n`**: the CRC takes the last n bits shifted into the ISR (for bits that do not
  pass the PAU: parity is a 1-bit CRC, poly 1).

Notes:

- When the queue lacks room (OUT) or bits (IN), the instruction waits.
- NRZ and NRZI decoders keep sampling an idle line, so the program decides where a frame ends.
- With diff, SE0 ends the frame.

## 9. Pins

- **Inputs are seen 2 clocks late** (synchronizer). A reply to an edge the SM makes itself comes
  back after at least ~4 clocks: sample late enough.
- **Shared latches**: one output latch and one direction latch for all SMs (direction 1 =
  output). After chip reset all pins are inputs with value 0.
- **PIN_MASK**: an SM changes only the pins in its PIN_MASK. If two SMs write the same pin in the
  same strobe, the higher SM wins. PAU TX drives over the latch.
- **Open drain** (GPIO [7:0]): the pin never drives high. Direction 1 and value 0 pulls it low;
  otherwise it floats (needs a pull-up).
- **Groups of pins** (in/out pins, set, side-set) go up from their base and stop at pin 7: bits
  past pin 7 are lost on write and read as 0.
- **Single pins** (`in_base n`, `out_base+1`, `in_base+1`) count mod 8.

## 10. Assembler (`sim/model/asm.py`)

Syntax: `label:` · comments `;` `//` `#` · numbers `12`, `0x0C`, `0b1100` · case-insensitive ·
max 64 instructions · `.word v` places a raw word.

| Directive | Sets |
|---|---|
| `.side_set n [opt] [pindirs]` · `.sideset_pins p` | SIDESET |
| `.in_pins p` · `.out_pins p` · `.set_pins p [count]` | PINBASE |
| `.pin_mask p, p, ...` | PIN_MASK |
| `.open_drain p, p, ...` | GPIO open drain (chip-wide; the host adds these pins) |
| `.shift_in left\|right` · `.shift_out left\|right` · `.push_thresh n` · `.pull_thresh n` | MISC |
| `.clkdiv int [frac]` | CLKDIV |
| `.cond0` .. `.cond3 condition` | COND_CFG |
| `.pau ...` · `.crc ...` | PAU_MODE, CRC_* |
| `.wrap_target` · `.wrap` | WRAP: the loop between them |
| `.start label` · `.x v` · `.y v` | PC, X, Y after RESET |

Fields that are not set keep their reset values (§5). The host can override any of them
(`host.load(sm, program, in_base=3, ...)`).

Example (UART TX: one byte per FIFO word, 8 strobes per bit):

```
.program uart_tx
.set_pins 0 1            ; TX = pin 0
.out_pins 0
    set pins, 1          ; idle high
    set pindirs, 1       ; pin 0 output
.wrap_target
    pull block           ; wait for a byte
    set pins, 0 [6]      ; start bit: 1 + 6 strobes, + the next set
    set x, 7
bit:
    out pins, 1 [6]      ; 8 data bits, LSB first
    jmp x--, bit
    set pins, 1 [6]      ; stop bit
.wrap
```

## 11. Watch out

- **IMEM** is written only while the SM is stopped.
- **`set` reaches 31 at most**: for larger constants use `.x`, `.y`, or a FIFO word.
- **FIFO speed**: PUSH and PULL see a 4-word front of the 132-word FIFO, which refills one word
  per clock. A program moving more than one word per clock can briefly wait.
- **Host data rate**: about 7.8 Mbit/s at a 50 MHz clock with long bursts.
- **Programs in this repo**: `programs/` holds 13 tested protocols (`programs/README.md`).
