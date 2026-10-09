# Protocol Verification Report: SPI Master Mode 2 (CPOL=1, CPHA=0)

## 1. Executive Summary
SPI Master Mode 2 (CPOL=1, CPHA=0) was verified on the openPIO architecture. In Mode 2, SCK idles high. Data is shifted onto MOSI while SCK is high and sampled on the falling edge of SCK. The program compiles to 6 instructions (18.75% IMEM occupancy) and exhibits clean clock and data phasing.

## 2. Protocol & Pin Architecture
- **Protocol**: SPI Master Mode 2 (CPOL=1, CPHA=0).
- **Clock Polarity (CPOL)**: 1 (SCK idles high).
- **Clock Phase (CPHA)**: 0 (Data sampled on falling SCK edge, shifted on rising SCK edge).
- **Pin Assignment**:
  - `GPIO 0`: SCK Output (side-set 0).
  - `GPIO 1`: MOSI Output (`out_base = 1`).
  - `GPIO 2`: MISO Input (`in_base = 2`).

## 3. Assembly Program Analysis
The program occupies 6 words (18.75% of IMEM):
- `pull block side 1`: Stalls on empty TX FIFO; SCK held high during idle.
- `set x, 7 side 1`: 8-bit loop counter; SCK held high.
- `out pins, 1 side 1 [1]`: Drives MOSI bit with SCK high for 2 cycles.
- `in pins, 1 side 0 [1]`: Transitions SCK low (falling edge), samples MISO, holds for 2 cycles.
- `jmp x--, bit_loop side 1`: Returns SCK high (rising edge) and decrements counter.
- `push block side 1`: Transfers received byte to RX FIFO with SCK remaining high.

## 4. Cycle/Timing Analysis
- **Bit Period**: 5 cycles per bit.
- **Clock Frequency**: $F_{PIO} / 5$.
- **Latency**: 43 cycles per 8-bit frame.

## 5. Verification Results
- **Full-Duplex Exchange**: PASS. Master sent `0xAA`, Slave sent `0x55`.
- **Clock Edge Alignment**: PASS. Falling-edge sampling confirmed.
- **IMEM Occupancy**: 6/32 instructions (18.75%).

## 6. RFC: ISA & Hardware Recommendations
- **RFC-SPI-3: Inverted Side-Set Directives or Polarity Invert Mask**:
  - *Observation*: Supporting inverted clock polarity requires modifying side-set literal values across instructions.
  - *Recommendation*: Introduce a state machine control bit (`INVERT_SIDESET`) to allow the same assembly routine to execute in both CPOL=0 and CPOL=1.
