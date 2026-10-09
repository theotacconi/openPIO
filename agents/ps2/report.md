# Protocol Verification Report: PS/2 Keyboard/Mouse Protocol

## 1. Executive Summary
A PS/2 keyboard and mouse protocol receiver was implemented and verified on the openPIO architecture. The receiver synchronizes to device-generated clock edges, tracking the complete 11-bit frame (Start bit, 8 Data bits LSB-first, Odd Parity bit, and Stop bit). Verification with standard scancodes (e.g. Key 'A' = `0x1C`) achieved 100% data integrity within 14 instructions (43.75% IMEM occupancy).

## 2. Protocol & Pin Architecture
- **Protocol**: IBM PS/2 Keyboard/Mouse Interface.
- **Physical Signaling**: Synchronous 2-wire open-drain interface with pull-up resistors. Clock is driven by the device (10 to 16.7 kHz).
- **Pin Assignment**:
  - `GPIO 0`: PS/2 CLK (sampled for level transitions).
  - `GPIO 1`: PS/2 DATA (sampled into ISR, `in_base = 1`).

## 3. Assembly Program Analysis
The program occupies 14 words (43.75% of IMEM):
- **Start Bit Synchronization**: `wait 0 gpio 0 ; wait 1 gpio 0` detects the initial start bit clock cycle.
- **Data Capture Loop**: Loops 8 times (`data_loop`). Stalls on `wait 0 gpio 0` until falling clock edge, captures DATA bit via `in pins, 1`, and waits for clock release (`wait 1 gpio 0`).
- **Parity Sampling**: Samples odd parity bit on the 10th clock pulse.
- **Stop Bit Verification**: Waits through the 11th clock pulse ensuring frame completion.
- `irq 0` / `push noblock`: Signals host CPU and enqueues the 9-bit payload into RX FIFO.

## 4. Cycle/Timing Analysis
- **Bit Cell Duration**: ~9 machine cycles per bit in simulation test bench.
- **Total Frame Latency**: 99 machine cycles per 11-bit scancode frame.

## 5. Verification Results
- **Scancode Capture (`0x1C`)**: PASS. 8 data bits and parity bit captured accurately.
- **Clock Edge Alignment**: PASS. Setup and hold times confirmed across falling clock edges.
- **IMEM Occupancy**: 14/32 instructions (43.75%).

## 6. RFC: ISA & Hardware Recommendations
- **RFC-PS2-1: Configurable Input Pin Glitch / Debounce Filter**:
  - *Observation*: PS/2 lines run over external cables and are prone to ringing and ground bounce. Currently, `wait 0 gpio 0` triggers immediately on any high-frequency spike.
  - *Recommendation*: Add a programmable 2-to-3 cycle digital filter / majority voter to GPIO input synchronizers to filter spurious edges.
