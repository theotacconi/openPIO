# Protocol Verification Report: LIN Master Protocol (Sync Break & PID Framing)

## 1. Executive Summary
A LIN (Local Interconnect Network) master header generator was synthesized and verified on the openPIO architecture. The engine emits a compliant physical LIN frame header consisting of a dominant Sync Break (>13 bit periods), a recessive Delimiter (>1 bit period), an alternating Sync Byte (`0x55`), and a serialized Protected Identifier (PID). The verification suite confirms standard timing and edge fidelity within 18 instructions (56.25% IMEM occupancy).

## 2. Protocol & Pin Architecture
- **Protocol**: LIN 2.x Physical Layer Header Generator.
- **Physical Signaling**: Single-wire open-collector/transceiver bus. Dominant level = 0V, Recessive level = Vbat (12V).
- **Pin Assignment**:
  - `GPIO 0`: LIN Transceiver TX (driven via `set pins` and `out pins`).
- **Timing**: 4 machine cycles per nominal bit time.

## 3. Assembly Program Analysis
The program occupies 18 words (56.25% of IMEM):
- **Sync Break Generation**: Uses a 13-iteration decrement loop with 3-cycle delay (`set pins, 0 [2] ; jmp x--`), generating 54 cycles (13.5 bit times) of continuous dominant low.
- **Break Delimiter**: Holds line recessive high for 4 cycles (`set pins, 1 [3]`).
- **Sync Field**: Serializes start bit (0), 8 alternating data bits (4 pairs of 1 and 0), and stop bit (1), matching the standard `0x55` baud calibration pattern.
- **PID Transmission**: Serializes start bit, shifts 8 bits of PID from the TX FIFO (`out pins, 1 [2]`), and asserts stop bit.
- **Completion Signaling**: Asserts `irq 0` to notify host CPU of header completion.

## 4. Cycle/Timing Analysis
- **Break Field Duration**: 54 cycles (13.5 bit times >= 13 bit times required by LIN spec).
- **Delimiter Duration**: 4 cycles (1.0 bit time >= 1 bit time required).
- **Sync Field Duration**: 40 cycles (10 bit times including start/stop bits).
- **Total Header Latency**: 145 machine cycles.

## 5. Verification Results
- **Break Field Conformance**: PASS (duration >= 52 cycles).
- **Delimiter Conformance**: PASS (duration >= 4 cycles).
- **Sync Field Alignment**: PASS (standard 0x55 bit pattern).
- **IMEM Occupancy**: 18/32 instructions (56.25%).

## 6. RFC: ISA & Hardware Recommendations
- **RFC-LIN-1: Hardware Auto-Parity Engine for PID Generation (PAU)**:
  - *Observation*: LIN PIDs require 2 parity bits (P0 = ID0^ID1^ID2^ID4, P1 = ~(ID1^ID3^ID4^ID5)). Generating this in software requires bit masks, shifts, and XORs that consume valuable program memory.
  - *Recommendation*: Implement the `PAU_CFG` Bit 2 ("Auto-Parity Insertion") in hardware so that 6-bit IDs automatically have LIN-compliant parity bits generated during output serialization.
