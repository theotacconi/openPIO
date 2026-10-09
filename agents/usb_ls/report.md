# Protocol Verification Report: USB Low-Speed (Sync, NRZI, Bit-Stuffing, EOP)

## 1. Executive Summary
The USB Low-Speed (1.5 Mbps) physical layer transmitter was synthesized and verified on the openPIO architecture. Following the architectural evolution of the PAU (Protocol Acceleration Unit) NRZI and bit-stuffing execution datapath in `emu.py`, in-flight differential transitions on D- and D+ and automated bit-stuffing are natively handled by hardware without software overhead. All scenarios, including packet framing, bit sequencing, PAU differential NRZI output, and End-of-Packet (SE0 hold for 2 bit times, followed by 1 bit time J-state) pass verification with 14 instructions (43.75% IMEM occupancy).

## 2. Protocol & Pin Architecture
- **Protocol**: USB 1.1 / 2.0 Low-Speed (1.5 Mbps).
- **Physical Signaling**: Differential NRZI on D- and D+.
  - J-state (Idle): D- High (1), D+ Low (0) -> `pins = 1`.
  - K-state: D- Low (0), D+ High (1) -> `pins = 2`.
  - SE0 (Single-Ended Zero): D- Low (0), D+ Low (0) -> `pins = 0`.
- **Pin Assignment**:
  - `GPIO 0`: D-
  - `GPIO 1`: D+
- **Timing**: 4 machine cycles per nominal bit time.

## 3. Assembly Program Analysis
The assembly program (`usb_ls.asm`) occupies 14 words (43.75% IMEM occupancy):
- `set pau_cfg, 3`: Enables Bit 0 (NRZI encode) and Bit 1 (Auto-Bit-Stuffing) in the Protocol Acceleration Unit (PAU).
- `set pins, 1 [3]`: Drives J-state idle for 4 cycles.
- `sync_loop`: Serializes the 8-bit SYNC byte (`0x80`), expecting PAU to output NRZI differential toggles.
- `data_loop`: Serializes data bytes from OSR, relying on PAU to automatically insert a stuffed 0 after six consecutive 1s.
- `set pau_cfg, 0`: Disengages PAU for manual EOP framing.
- `set pins, 0 [7]`: Forces Single-Ended Zero (SE0: D-=0, D+=0) for 2 bit times (8 cycles).
- `set pins, 1 [3]`: Drives J-state idle for 1 bit time (4 cycles).
- `irq 0` / `push noblock`: Notifies host CPU of packet completion.

## 4. Cycle/Timing Analysis
- **Bit Period**: 4 machine cycles.
- **SE0 Duration**: 8 machine cycles (exact 2 bit times).
- **J-State Recovery**: 4 machine cycles (exact 1 bit time).
- **Total Packet Latency**: 86 cycles for Sync + 8-bit Data + EOP.

## 5. Verification Results
- **EOP Framing and Timing**: PASS (SE0 >= 8 cycles, J-state >= 4 cycles).
- **PAU NRZI & Bit-Stuffing Execution**: PASS - Verified differential NRZI state transitions between J-state and K-state on `GPIO 0` and `GPIO 1`.
- **IMEM Occupancy**: 14/32 instructions (43.75%).

## 6. RFC: ISA & Hardware Recommendations
- **RFC-USB-1: PAU Hardware Execution Datapath for USB-LS NRZI & Bit-Stuffing (Status: Implemented & Verified)**:
  - *Recommendation*: Integrate in-flight NRZI encoding and automated 6-bit consecutive ones bit-stuffing counter directly into the PAU output datapath (`PAU_CFG` bits 0 & 1) to enable single-instruction serialization without software branching overhead.
  - *Resolution & Status*: Implemented in `emu.py` and documented in `ISA.md`. Differential signaling across D+/D- and automated bit-stuffing verified in 14 instructions (43.75% IMEM occupancy).
