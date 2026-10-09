# Protocol Verification Report: IEEE 1149.1 JTAG TAP Controller

## 1. Executive Summary
An IEEE 1149.1 Standard Test Access Port (JTAG) state machine controller was implemented and verified on openPIO. The controller drives Test Clock (TCK) via side-set, manipulates Test Mode Select (TMS) to navigate TAP states (Reset -> Run-Test/Idle -> Select-DR-Scan -> Capture-DR -> Shift-DR -> Exit1-DR -> Update-DR -> Idle), and serializes Test Data In (TDI) while sampling Test Data Out (TDO). All transitions were validated against a cycle-accurate IEEE 1149.1 FSM within 25 instructions (78.12% IMEM occupancy).

## 2. Protocol & Pin Architecture
- **Protocol**: IEEE 1149.1 Standard TAP Controller.
- **Physical Signaling**: Synchronous 4-wire interface.
- **Pin Assignment**:
  - `GPIO 0`: TCK (Test Clock, side-set base 0).
  - `GPIO 1`: TMS (Test Mode Select, bit 1 via `set pins`).
  - `GPIO 2`: TDI (Test Data In, bit 2 via `out pins`).
  - `GPIO 3`: TDO (Test Data Out, sampled via `in pins`, `in_base = 3`).

## 3. Assembly Program Analysis
The program occupies 25 words (78.12% of IMEM):
- **TAP Reset Loop**: 5 clock edges with TMS=1 (`set pins, 2 side 0 ; nop side 1`) to guarantee entry into `Test-Logic-Reset`.
- **Navigation Sequence**: Step-by-step TCK clocking with TMS=0 (Run-Test/Idle), TMS=1 (Select-DR), TMS=0 (Capture-DR), TMS=0 (Shift-DR).
- **Shift-DR Loop**: Shifts 7 data bits on TDI while sampling TDO on TCK rising edges with TMS held at 0.
- **Exit1 Transition**: Shifts the 8th bit while simultaneously raising TMS to 1 to exit Shift-DR.
- **Update and Park**: Clocks through Update-DR (TMS=1) and returns to Run-Test/Idle (TMS=0).
- `push noblock side 0`: Delivers scanned TDO word to RX FIFO.

## 4. Cycle/Timing Analysis
- **Clock Period**: 4 machine cycles per TCK period (2 cycles low, 2 cycles high).
- **Reset Duration**: 20 cycles (5 TCK periods).
- **Total Scan Transaction Latency**: 92 machine cycles.

## 5. Verification Results
- **TAP FSM State Compliance**: PASS. Exact IEEE 1149.1 state trajectory confirmed without invalid branches.
- **Shift-DR In/Out Alignment**: PASS. Correct TDO sampling on rising TCK edge.
- **IMEM Occupancy**: 25/32 instructions (78.12%).

## 6. RFC: ISA & Hardware Recommendations
- **RFC-JTAG-1: Multi-Pin Side-Set Directives (TCK + TMS Dual Side-Set)**:
  - *Observation*: Navigating TAP states requires executing discrete `set pins` instructions to change TMS before toggling TCK with side-set. This inflates IMEM occupancy to 25 words.
  - *Recommendation*: Support a 2-bit side-set configuration (`.side_set 2`) mapped to `{TMS, TCK}`. This would allow state machine navigation to occur in zero instruction overhead, shrinking the JTAG program to ~8 instructions.
