# openPIO host interface v2

The host (a microcontroller, e.g. the RP2040 on the Tiny Tapeout demo board) loads programs,
configures and runs the state machines (SMs) and moves data over an SPI slave. Instruction set:
ISA.md. Reference model: `sim/model/chip.py`.

## SPI

- Pins: `ui_in[0]` SCLK, `ui_in[1]` MOSI, `ui_in[2]` CS_N, `uo_out[0]` MISO.
- Mode 0 (CPOL = 0, CPHA = 0), MSB first, sampled by the system clock: SCLK high and low phases
  of at least 3 `clk` cycles (SCLK <= `clk` / 6, 8.3 MHz at 50 MHz).
- A transaction is CS_N low, a 16-bit header, then any number of 16-bit data words (a burst),
  then CS_N high. A word is executed when its last bit is in.

```
header:  15  14  13 | 12  11 | 10 .. 6 | 5 .. 0
            Op      |   SM   |    0    |  Addr
```

| Op | Command | Each data word |
|---|---|---|
| `000` | NOP | ignored |
| `001` | reserved | ignored |
| `010` | REG_WR | register `Addr` of SM `SM` <= word, then Addr + 1 |
| `011` | REG_RD | MISO = register `Addr`, then Addr + 1 |
| `100` | TX_PUSH | TX FIFO <= word (dropped when full; ISA.md, FIFOs) |
| `101` | RX_POP | MISO = RX FIFO head, popped (0 when empty) |
| `110` | IMEM_WR | instruction `Addr` <= word, then Addr + 1 (SM stopped) |
| `111` | IMEM_RD | MISO = instruction `Addr`, then Addr + 1 (SM stopped; 0 while it runs: the port fetches) |

MISO carries the read data during the data words of REG_RD, RX_POP and IMEM_RD (the value is
taken when the word starts) and the SM's STATUS register during every other word and during
the header. Chip registers (`Addr` >= 0x20) ignore `SM`.

## Registers of an SM (`Addr` 0x00..0x1F)

| Addr | Name | R/W | Fields (reset value) |
|---|---|---|---|
| 0x00 | STATUS | R | whole FIFOs (ISA.md, FIFOs): [0] TX empty, [1] TX full, [2] RX empty, [3] RX full, [4] stalled, [5] enabled, [13:8] PC |
| 0x01 | WRAP | RW | [5:0] wrap_bottom (0), [13:8] wrap_top (63) |
| 0x02 | SIDESET | RW | [2:0] count (0), [3] opt (0), [4] drives pindirs (0), [10:8] base (0) |
| 0x03 | PINBASE | RW | [2:0] in_base, [6:4] out_base, [10:8] set_base (0), [14:12] set_count (5) |
| 0x04 | PIN_MASK | RW | [7:0] pins this SM may change (0xFF) |
| 0x05 | MISC | RW | [0] ISR shift right (1), [1] OSR shift right (1), [11:8] push threshold (0 = 16), [15:12] pull threshold (0 = 16) |
| 0x06 | CLKDIV | RW | strobe divider: [7:0] integer (1; 0 = 1), [15:8] fraction / 256 (0); loaded on RESET |
| 0x07 | COND_CFG | W | `{Slot[9:8], condition[7:0]}`, one condition per write (ISA.md, Conditions) |
| 0x08 | COND_CFG01 | R | COND_CFG slots 1 [15:8], 0 [7:0] (0x6160) |
| 0x09 | COND_CFG23 | R | slots 3, 2 (0xE400) |
| 0x0A | PAU_MODE | RW | ISA.md, PAU (0) |
| 0x0B | CRC_CFG | RW | ISA.md, PAU (0) |
| 0x0C | CRC_POLY_LO | RW | CRC_POLY [15:0] |
| 0x0D | CRC_POLY_HI | RW | CRC_POLY [31:16] |
| 0x0E | CRC_RES_LO | RW | CRC_RES [15:0] |
| 0x0F | CRC_RES_HI | RW | CRC_RES [31:16] |
| 0x10 | IRQ | RW | read: flags [7:0]; write: [7:0] clear the flags whose bit is 1, then [15:8] set them (the host can start a program waiting on a flag) |
| 0x11 | PC | RW* | [5:0] |
| 0x12 | X | RW* | |
| 0x13 | Y | RW* | |
| 0x14 | ISR | RW* | |
| 0x15 | OSR | RW* | |
| 0x16 | COUNTS | RW* | [4:0] isr_count, [12:8] osr_count |
| 0x17 | COND01 | R | COND (live) slots 1, 0 |
| 0x18 | COND23 | R | COND slots 3, 2 |
| 0x19 | CRC_LO | R | CRC [15:0] |
| 0x1A | CRC_HI | R | CRC [31:16] |
| 0x1B | SM_STATUS | R | the STATUS operand (ISA.md): SM status [7:0], PAU status [15:8] |
| 0x1C | FIFO_LEVEL | R | words in the TX FIFO [7:0], in the RX FIFO [15:8] (0..133; ISA.md, FIFOs) |
| 0x1D | IRQ_MASK | RW | what reaches HOST_IRQ: [7:0] these flags when set (0xFF), [8] RX FIFO not empty (0), [9] TX FIFO at most 64 words (0) |

RW*: written only while the SM is stopped (CTRL EN = 0); writes while it runs are ignored.

## Chip registers (`Addr` 0x20..0x3F, any `SM`)

| Addr | Name | R/W | Fields |
|---|---|---|---|
| 0x20 | CTRL | RW | [3:0] EN: SM i runs while bit i is 1 (0). Write only: [7:4] RESET, [11:8] STEP, one bit per SM |
| 0x21 | GPIO | RW | [7:0] open-drain mask (0), [15:11] `ui_in[7:3]` replaces `gpio_in[7:3]` (0) |
| 0x22 | PINS | R | [7:0] pin values driven, [15:8] pin output enables |
| 0x23 | PINS_IN | R | [7:0] `gpio_in` |
| 0x24 | IRQ_SUM | R | bit i: SM i requests HOST_IRQ (its IRQ_MASK) |
| 0x25 | ID | R | [15:12] ISA version (2), [11:8] number of SMs, [7:0] 0x50 ('P') |

- **RESET** (bit i): SM i as CTRL.RESET in ISA.md: registers, IRQ flags, FIFOs, PAU, PC = 0,
  COND = COND_CFG, DLYMUL = 1, CRC = its initial value, CLKDIV and PAU_BAUD reloaded.
  Configuration, IMEM and the pin latches (shared) are kept.
- **STEP** (bit i, SM stopped): SM i runs until it has executed one strobe.
- A CTRL write sets EN to its bits [3:0] and applies RESET then STEP in the same clock: SMs
  enabled together start on the same clock.

## Pins

- `uio[7:0]`: value = pin latch, output enable = direction latch, both overridden by an active
  PAU TX and by open drain (ISA.md, Multiple state machines). `gpio_in` = `uio_in`, except bits
  [7:3] taken from `ui_in[7:3]` where GPIO selects them. All inputs pass a 2-flip-flop
  synchronizer.
- `uo_out[1]` HOST_IRQ: high while any SM requests it through its IRQ_MASK: a flag set (flags
  SMs use only between themselves stay off it), RX FIFO not empty, TX FIFO at most 64 words.
  The FIFO requests end by themselves once the host has drained or refilled the FIFO.
- `uo_out[7:2]`: pin values [7:2] as push-pull outputs.

## Host data rate

SCLK <= `clk` / 6 and a burst of n words costs 16 (n + 1) SCLK periods: about 7.8 Mbit/s of data
at 50 MHz for long bursts (v1: 2.8 Mbit/s, SCLK <= `clk` / 12 and 24 bits per word).
