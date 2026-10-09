<!---
Tiny Tapeout datasheet page for openPIO. Rendered by the TT docs flow.
-->

## How it works

openPIO is a single programmable I/O state machine: a tiny 16-bit processor whose instruction set exists only to move bits between pins and FIFOs with cycle-exact timing. A host loads a program of up to 32 instructions over SPI, then the state machine bit-bangs a serial protocol (UART, SPI, I2C, 1-Wire, JTAG, SWD, CAN, LIN, PS/2, Manchester, USB low-speed, ...) on the 8 bidirectional pins with no software jitter.

- **Datapath:** 16-bit X and Y scratch registers, 16-bit input and output shift registers (ISR/OSR) with bit counters, and an ALU (MOV, XOR, ADD, bit-reverse). Each instruction carries a 5-bit field shared between a delay (0-31 cycles) and side-set pins.
- **Instructions:** JMP (conditions on X, Y, pins, timeout, OSR), WAIT (pin/IRQ level or edge, with an optional timeout), IN, OUT, PUSH/PULL, ALU, IRQ and SET. The full specification is `sim/model/ISA.md` in the project repository.
- **PAU (Protocol Acceleration Unit):** optional line coders on `OUT PINS`: a Manchester transmitter with a 17-symbol buffer, and NRZI with automatic USB bit-stuffing.
- **Clocking:** a 5-bit integer + 8-bit fractional clock prescaler; every delay is scaled by the integer divider.
- **FIFOs:** 4 x 16-bit TX (host to state machine) and 4 x 16-bit RX (state machine to host).

### SPI host interface

SPI mode 0 (CPOL = 0, CPHA = 0), MSB first, sampled by the system clock: **SCLK high and low phases must each last at least 6 `clk` cycles** (SCLK <= 4 MHz at 50 MHz).

Each frame is 24 bits while CS_n is low: an 8-bit command `{op[2:0], addr[4:0]}` then 16 data bits. Frames may run back-to-back under one CS_n. For reads, MISO carries the response during the 16 data bits; for every other command it carries the STATUS register.

| op  | Command | Effect |
|-----|---------|--------|
| 001 | IMEM_WR | instruction word `addr` <= data (only while `CTRL.EN` = 0) |
| 010 | REG_WR  | register `addr` <= data |
| 011 | REG_RD  | response = register `addr` |
| 100 | TX_PUSH | TX FIFO <= data (dropped if full) |
| 101 | RX_POP  | response = RX FIFO head, popped (0 if empty) |
| 110 | IRQ     | response = {peer flags, local flags}; flags whose data bit is 1 are cleared |

| addr | Register | Fields (reset value) |
|------|----------|----------------------|
| 0  | CTRL    | [0] EN (0), [1] RESET (0): while 1, the state machine and FIFOs are held in reset |
| 1  | WRAP    | [4:0] wrap_bottom (0), [12:8] wrap_top (31) |
| 2  | SIDESET | [2:0] count (0), [3] optional (0), [4] drives pindirs (0), [10:8] base (0) |
| 3  | PINBASE | [2:0] in_base, [6:4] out_base, [10:8] set_base (all 0), [14:12] set_count (5) |
| 4  | MISC    | [2:0] jmp_pin (0), [4] ISR shift right (1), [5] OSR shift right (1) |
| 5  | CLKDIV  | [4:0] integer divider (1), [15:8] fraction (0); loaded on RESET |
| 6  | GPIO    | [7:0] open-drain mask (0), [15:11] use `ui_in[7:3]` as `gpio_in[7:3]` (0) |
| 8  | STATUS  | [0] TX empty, [1] TX full, [2] RX empty, [3] RX full, [4] stalled, [5] timeout flag, [12:8] PC (read-only) |
| 9-12 | X, Y, ISR, OSR | read-only |
| 13 | PINS    | [7:0] gpio_out, [15:8] pindirs (read-only) |

### Pins

- `uio[7:0]` are the state machine's GPIO: output value = `gpio_out`, output enable = `pindirs`, input = pad value. Open-drain pins (GPIO register mask) drive low or float, so I2C, 1-Wire and SWD work with external pull-ups.
- `ui_in[7:3]` can replace `uio_in[7:3]` as inputs, bit by bit.
- `uo_out[7:2]` mirror `gpio_out[7:2]` as push-pull outputs.
- `uo_out[1]` (HOST_IRQ) is high while any local IRQ flag is set.
- All GPIO inputs pass a 2-flip-flop synchronizer, so the state machine sees pins 2 clock cycles late.

## How to test

You need an SPI master (microcontroller, RP2040 on the TT demo board, or a USB-SPI adapter) on `ui_in[2:0]` / `uo_out[0]`.

1. Reset the design (`rst_n` low, then high).
2. Load the program: one `IMEM_WR` frame per instruction, `cmd = 0x20 | addr`. Write all 32 words; unused ones can be 0.
3. Configure: `REG_WR` (`cmd = 0x40 | addr`) to WRAP, SIDESET, PINBASE, MISC, CLKDIV and GPIO as the program requires.
4. Write CTRL = 0x0002 (RESET; this loads CLKDIV and clears the FIFOs), then CTRL = 0x0001 (EN).
5. Exchange data with `TX_PUSH` (`cmd = 0x80`) and `RX_POP` (`cmd = 0xA0`), poll STATUS with `REG_RD` (`cmd = 0x68`), and read or clear IRQ flags with `IRQ` (`cmd = 0xC0`), or use the HOST_IRQ pin.
6. Before loading a new program, write CTRL = 0x0000.

Example, a loop that echoes every TX word back to RX (`pull block`, `mov isr, osr`, `push block`):

```
IMEM_WR 0: 0x80C0
IMEM_WR 1: 0xA085
IMEM_WR 2: 0x8040
IMEM_WR 3..31: 0x0000
REG_WR WRAP:  0x0200   (wrap_top = 2)
REG_WR CTRL:  0x0002, then 0x0001
TX_PUSH 0x1234  ->  RX_POP returns 0x1234
```

Assembled programs for 14 protocols are in `agents/*/*.asm` and can be built with `sim/model/asm.py`.

## External hardware

- An SPI master to load and run programs (MCU or USB-SPI adapter, SCLK <= `clk` / 12).
- Pull-up resistors (e.g. 4.7 kOhm) on any `uio` pin used as an open-drain bus (I2C, 1-Wire, SWD).
- Whatever the chosen protocol talks to (UART adapter, SPI/I2C peripheral, ...).
