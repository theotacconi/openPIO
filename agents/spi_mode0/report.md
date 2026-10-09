# Protocol Verification Report: SPI Master Mode 0 (CPOL=0, CPHA=0)

## 1. Executive Summary
The Serial Peripheral Interface (SPI) Master operating in Mode 0 (CPOL=0, CPHA=0) was successfully synthesized, compiled, and verified against the openPIO architecture. Utilizing a single dedicated side-set bit for the Serial Clock (SCK), the core executes full-duplex transfers with deterministic phase alignment in 6 instructions (18.75% IMEM occupancy). Physical line interactions with a simulated slave device verified clean setup and hold times on both MOSI and MISO lines.

## 2. Protocol & Pin Architecture
- **Protocol**: SPI Master Mode 0 (CPOL=0, CPHA=0).
- **Clock Polarity (CPOL)**: 0 (SCK idles low).
- **Clock Phase (CPHA)**: 0 (Data is driven on falling SCK edge, sampled on rising SCK edge).
- **Pin Assignment**:
  - `GPIO 0`: SCK Output (driven via side-set, `sideset_base = 0`).
  - `GPIO 1`: MOSI Output (driven via `out pins, 1`, `out_base = 1`).
  - `GPIO 2`: MISO Input (sampled via `in pins, 1`, `in_base = 2`).

## 3. Assembly Program Analysis
The program occupies 6 words (18.75% of IMEM):
- `pull block side 0`: Stalls execution until host CPU enqueues transmit data into TX FIFO; guarantees SCK remains low during idle.
- `set x, 7 side 0`: Configures 8-bit frame counter in register X while holding SCK low.
- `out pins, 1 side 0 [1]`: Shifts MSB from OSR onto MOSI line while maintaining SCK at low level for 2 cycles (setup time).
- `in pins, 1 side 1 [1]`: Drives SCK high (rising edge) and captures MISO into ISR, holding clock high for 2 cycles.
- `jmp x--, bit_loop side 0`: Drives SCK low (falling edge) and decrements counter; loops for 8 bits.
- `push block side 0`: Enqueues assembled RX payload into RX FIFO and returns SCK to idle.

## 4. Cycle/Timing Analysis
- **Bit Period**: 5 cycles per bit (2 cycles SCK low, 2 cycles SCK high, 1 cycle branch).
- **Clock Frequency**: $F_{PIO} / 5$.
- **Duty Cycle**: ~40% High / 60% Low due to 1-cycle JMP branch overhead.
- **Total Frame Latency**: 43 machine cycles per 8-bit exchange.

## 5. Verification Results
- **Full-Duplex Exchange**: PASS. Master sent `0xA5`, Slave sent `0x3C`. Both bytes received with zero bit errors.
- **Clock Polarity Verification**: PASS. SCK remained low during idle and stall conditions.
- **IMEM Occupancy**: 6/32 instructions (18.75%).

## 6. RFC: ISA & Hardware Recommendations
- **RFC-SPI-1: Symmetric Clock Divider with Side-Set Delay Balancing**:
  - *Observation*: The 1-cycle branch latency creates a 3:2 asymmetric clock duty cycle (5 cycles/bit).
  - *Recommendation*: Support auto-decrement loop hardware or side-set delay mapping that maintains 50% duty cycle clock generation.
