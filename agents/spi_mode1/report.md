# Protocol Verification Report: SPI Master Mode 1 (CPOL=0, CPHA=1)

## 1. Executive Summary
SPI Master Mode 1 (CPOL=0, CPHA=1) was implemented and verified on the openPIO architecture. In this mode, the clock idles low, data transitions occur on the rising edge of SCK, and data sampling occurs on the falling edge. The implementation requires 6 instructions (18.75% IMEM occupancy) and executed with full physical line fidelity in verification test benches.

## 2. Protocol & Pin Architecture
- **Protocol**: SPI Master Mode 1 (CPOL=0, CPHA=1).
- **Clock Polarity (CPOL)**: 0 (SCK idles low).
- **Clock Phase (CPHA)**: 1 (Data is driven on rising SCK edge, sampled on falling SCK edge).
- **Pin Assignment**:
  - `GPIO 0`: SCK Output (side-set base 0).
  - `GPIO 1`: MOSI Output (`out_base = 1`).
  - `GPIO 2`: MISO Input (`in_base = 2`).

## 3. Assembly Program Analysis
The program occupies 6 words (18.75% of IMEM):
- `pull block side 0`: Stalls on empty TX FIFO with SCK low.
- `set x, 7 side 0`: Sets 8-bit loop iteration counter with SCK low.
- `out pins, 1 side 1 [1]`: Shifts MOSI bit out concurrently with SCK rising edge (side 1); holds for 2 cycles.
- `in pins, 1 side 0 [1]`: Samples MISO on SCK falling edge (side 0); holds for 2 cycles.
- `jmp x--, bit_loop side 0`: Decrements bit counter while maintaining SCK low.
- `push block side 0`: Pushes received byte into RX FIFO and maintains idle clock state.

## 4. Cycle/Timing Analysis
- **Bit Period**: 5 cycles per bit.
- **Clock Frequency**: $F_{PIO} / 5$.
- **Latency**: 43 cycles per 8-bit frame.

## 5. Verification Results
- **Full-Duplex Exchange**: PASS. Master transmitted `0x5A`, Slave responded with `0xC3`.
- **Clock Edge Alignment**: PASS. Setup and hold times confirmed across rising and falling edges.
- **IMEM Occupancy**: 6/32 instructions (18.75%).

## 6. RFC: ISA & Hardware Recommendations
- **RFC-SPI-2: Unified Configurable SPI Hardware Engine in PAU**:
  - *Observation*: While 6 instructions is efficient, having separate firmware for 4 SPI modes consumes program space when switching between peripherals dynamically.
  - *Recommendation*: Add a mode bitfield into `PAU_CFG` to handle CPOL/CPHA directly in the output serializer.
