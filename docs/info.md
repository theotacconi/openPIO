<!---
Tiny Tapeout datasheet page for openPIO. Rendered by the TT docs flow.
Exact rules: sim/model/ISA.md (instruction set) and sim/model/HOST.md (host interface).
-->

## How it works

openPIO is a programmable I/O co-processor: two state machines (SMs), each a tiny 16-bit processor whose instruction set only moves bits between pins and FIFOs with cycle-exact timing. A host microcontroller loads a program of up to 64 instructions per SM over SPI; the SMs then run serial protocols on up to 14 pins (8 bidirectional) with no software jitter.

- **Datapath per SM:** 16-bit X and Y registers, input and output shift registers (ISR / OSR) with autopush / autopull, an 8-operation ALU (MOV, REV, AND, OR, XOR, XNOR, ADD, SUB). Each instruction has a 5-bit field shared between a delay and side-set pins.
- **Instructions:** JMP on four programmable conditions (any pin, IRQ flag, register bit, SM or PAU status), WAIT on a level or an edge with an optional timeout, IN, OUT (also to the PC: jump tables), ALU, PUSH / PULL (also to and from the other SM), IRQ and SET.
- **PAU (Protocol Acceleration Unit):** a serial transmitter and receiver with its own bit timing between OUT / IN PINS and the pins: NRZ, NRZI, Manchester and biphase mark line codes, programmable bit stuffing, a CRC of 1 to 32 bits, clock recovery and a line monitor that stops the transmitter at the first bit another node overrides (CAN, LIN, 1-Wire arbitration).
- **Clocking:** a 16-bit integer + 8-bit fractional prescaler per SM (from 50 MHz down to 763 Hz); delays and timeouts count strobes times a delay multiplier.
- **FIFOs:** 133-word TX (host to SM) and RX (SM to host) FIFOs per SM, in an SRAM.
- **Two SMs** share the pins (per-SM pin mask), start on the same clock, and signal each other through IRQ flags.

Example programs (UART, Modbus RTU, SPI, I2C, USB low speed, USB Power Delivery BMC, 10BASE-T TX, CAN, LIN, DALI, HDLC, JTAG, SWD, 1-Wire, PS/2, I2S, WS2812B, NEC infrared, quadrature encoder, a logic analyzer, ...) are in `programs/`; `sim/model/asm.py` assembles them.

### SPI host interface

SPI mode 0, MSB first, sampled by the system clock: SCLK high and low phases of at least 3 `clk` cycles (SCLK <= `clk` / 6, 8.3 MHz at 50 MHz).

A transaction is CS_N low, a 16-bit header `{Op[15:13], SM[12:11], 0[10:6], Addr[5:0]}`, any number of 16-bit data words, CS_N high. Addresses increment within a burst. MISO carries the read data during the data words of reads and the SM's STATUS register otherwise (during the header: the STATUS of the previous transaction's SM).

| Op | Command | Each data word |
|----|---------|----------------|
| 010 | REG_WR | register `Addr` <= word |
| 011 | REG_RD | MISO = register `Addr` |
| 100 | TX_PUSH | TX FIFO <= word (dropped when full) |
| 101 | RX_POP | MISO = RX FIFO head, popped (0 when empty); `Addr` = words to pop, 1..63 (0 = 64) |
| 110 | IMEM_WR | instruction `Addr` <= word (SM stopped) |
| 111 | IMEM_RD | MISO = instruction `Addr` (SM stopped) |

Registers 0x00-0x1F belong to the SM named in the header (configuration, PC, X, Y, CRC, FIFO levels, IRQ flags); 0x20-0x29 are chip registers: CTRL (0x20: [3:0] enable, [7:4] reset, [11:8] single step, one bit per SM), GPIO (open-drain mask), pin state, ID (0x26, reads 0x2250) and the pads of each pin.

### Pins

The instruction set has 16 pin positions; pin k is `uio[k]`, pin 8 + k is `uo_out[k]` (output) and `ui_in[k]` (input):

| Pin | Output | Input | Kind |
|-----|--------|-------|------|
| 0-7 | `uio_out[k]` | `uio_in[k]` | bidirectional, direction per pin, open drain |
| 8, 9 | - | - | used by the host interface |
| 10 | `uo_out[2]` | - | output only |
| 11-15 | `uo_out[3..7]` | `ui_in[3..7]` | output and input on separate pads |

- Values come from the SMs' pin latches or a PAU transmitter. Open-drain pins drive low or float, for I2C, 1-Wire and SWD with external pull-ups: pins 0-7 only, as are bidirectional buses and the PAU line monitor.
- `uo_out[1]` HOST_IRQ is high while an SM requests attention: an IRQ flag, RX FIFO not empty or TX FIFO at most 64 words, as each SM's IRQ_MASK selects.
- Every input passes a 2-flip-flop synchronizer: the SMs see pins 2 clocks late.

### Clock

`clk` runs at up to 50 MHz, signed off in the typical corner (1.2 V, 25 C), with 4.0 ns of setup slack. In the slow corner (1.08 V, 125 C) it closes at 40 MHz. Hold is met in all corners.

## How to test

You need an SPI master on `ui_in[2:0]` / `uo_out[0]`: the RP2040 of the demo board, another microcontroller or a USB-SPI adapter. `sim/model/host.py` is a reference driver.

1. Reset the design (`rst_n` low, then high).
2. Write the program with IMEM_WR (header 0xC000 for SM 0 from address 0), then its configuration with REG_WR. `asm.py` produces both from one source file.
3. Write CTRL: reset and enable the SMs together.
4. Exchange data with TX_PUSH and RX_POP; poll STATUS or use HOST_IRQ.
5. Clear the enable bit before loading a new program.

Echo loop on SM 0 (`pull block`, `mov isr, osr`, `push block`): every TX word comes back on RX.

```
0xC000  0xC070 0x8085 0xC050   IMEM_WR SM 0 from 0: the three instructions
0x4001  0x0200                 REG_WR WRAP: wrap_top = 2
0x4020  0x0011                 REG_WR CTRL: reset and enable SM 0
0x8000  0x1234                 TX_PUSH
0xA001  0x0000                 RX_POP of 1 word: MISO returns 0x1234
```

## External hardware

- An SPI master to load and run programs.
- Pull-up resistors (e.g. 4.7 kOhm) on any `uio` pin used as an open-drain bus (I2C, 1-Wire, SWD).
- Pins 11-15 read `ui_in[3..7]`. On the demo board these also reach the RP2040 and the input DIP switches: the RP2040 firmware leaves them undriven, the switches stay off, and external signals come in through the input Pmod.
- Whatever the chosen protocol talks to: a UART adapter, SPI / I2C peripherals, a USB low-speed device, 10BASE-T magnetics, a CAN or LIN transceiver, ...
