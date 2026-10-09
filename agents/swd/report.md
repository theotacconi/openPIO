# Protocol Verification Report: ARM Serial Wire Debug (SWD) Host Protocol

## 1. Executive Summary
An ARM Serial Wire Debug (SWD) host interface was synthesized and cycle-accurately verified on openPIO. The controller manages the two-wire physical layer (SWCLK and bidirectional SWDIO), orchestrating an 8-bit host packet request, a strictly timed Turnaround (Trn) high-impedance cycle, a 3-bit Target ACK phase (sampling ACK OK `001b`), and a secondary turnaround cycle back to host drive. Verification confirms cycle-accurate direction switching within 15 instructions (46.88% IMEM occupancy).

## 2. Protocol & Pin Architecture
- **Protocol**: ARM SWD (Serial Wire Debug) Physical Layer.
- **Physical Signaling**: Synchronous 2-wire interface. SWCLK is driven by the host; SWDIO is bidirectional.
- **Pin Assignment**:
  - `GPIO 0`: SWCLK Output (side-set base 0).
  - `GPIO 1`: SWDIO Bidirectional Data (`out_base = 1`, `in_base = 1`, `set_base = 0`).

## 3. Assembly Program Analysis
The program occupies 15 words (46.88% of IMEM):
- `pull block side 0`: Stalls on TX FIFO starvation while holding SWCLK low.
- `set pindirs, 2 side 0`: Asserts SWDIO as output (`pindirs` bit 1 = 1) with SWCLK low.
- `req_loop`: 8-bit transmission loop. Shifts request bits onto SWDIO on SWCLK falling edge; holds for rising edge.
- **Turnaround Cycle (Trn)**: `set pindirs, 0 side 0 [1]` instantly releases SWDIO to high-Z input mode on the falling clock edge, followed by `nop side 1 [1]` to complete the 1-cycle turnaround float window.
- `ack_loop`: 3-bit ACK reception loop. Clocks SWCLK high (`in pins, 1 side 1 [1]`) and captures Target response into ISR.
- **Second Turnaround**: `set pindirs, 2 side 1 [1]` reclaims line control for host drive.
- `push noblock side 0`: Transfers 3-bit ACK to RX FIFO.

## 4. Cycle/Timing Analysis
- **Bit Period**: 5 machine cycles per clock cycle.
- **Turnaround Period**: Exactly 1 SWCLK cycle (5 machine cycles of line float).
- **Target ACK Sampling**: Sampled at cycles 50, 55, and 60.
- **Total Request + ACK Latency**: 68 machine cycles.

## 5. Verification Results
- **Request Framing**: PASS. 8-bit command transmitted with proper setup and hold.
- **Turnaround Line Float**: PASS. SWDIO released to input during Trn cycle (verified `pindirs & 2 == 0`).
- **ACK OK Reception**: PASS. Target ACK `001b` captured and extracted without bit distortion.
- **IMEM Occupancy**: 15/32 instructions (46.88%).

## 6. RFC: ISA & Hardware Recommendations
- **RFC-SWD-1: Simultaneous Clock Side-Set and PINDIRS Direction Switching**:
  - *Observation*: During bidirectional turnaround, managing both line direction (`pindirs`) and clock polarity (`gpio_out`) currently requires coordinating side-set with `set pindirs`.
  - *Recommendation*: Allow side-set to drive pin directions, enabling single-instruction bus turnaround transitions.
