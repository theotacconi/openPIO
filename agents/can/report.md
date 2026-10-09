# Protocol Verification Report: CAN Bit-Level Controller (Arbitration & Collision)

## 1. Executive Summary
A Controller Area Network (CAN) bit-level serializer with non-destructive bitwise arbitration was synthesized and validated on openPIO. The implementation drives Dominant (0) and Recessive (1) bit states onto the physical bus and samples the transceiver receive line on every transmitted bit. When a competing node transmits a dominant bit with higher priority, the openPIO controller detects the bus collision, immediately halts transmission, switches to recessive state, and asserts `IRQ 1`. Both uncontested transmission and arbitration loss scenarios passed verification within 17 instructions (53.12% IMEM occupancy).

## 2. Protocol & Pin Architecture
- **Protocol**: CAN (Controller Area Network) 2.0A / 2.0B physical arbitration layer.
- **Physical Signaling**: Wired-AND / Differential transceiver. Dominant (0) actively drives bus low; Recessive (1) is passive/floating.
- **Pin Assignment**:
  - `GPIO 0`: TX (Transceiver transmit input). Driven via `set pins`.
  - `GPIO 1`: RX (Transceiver receive output / bus monitor). Mapped to `jmp_pin = 1`.

## 3. Assembly Program Analysis
The assembly program occupies 17 words (53.12% of IMEM):
- `pull block`: Waits for 11-bit CAN frame identifier.
- `set pins, 1`: Maintains recessive bus state prior to transmission.
- `set x, 10`: Loads 11 identifier bit counter into register X.
- `out y, 1`: Shifts current bit into register Y.
- `jmp !y, send_dominant`: If bit is 0, branches to dominant driver.
- `set pins, 1 [2]`: Drives recessive (1) and holds for bus settling.
- `jmp pin, arb_ok`: Inspects RX pin via `jmp_pin`. If high, node retains arbitration.
- `irq 1` / `set pins, 1` / `jmp arb_exit`: If RX is low (collision), raises collision `IRQ 1`, releases TX immediately to recessive state, and aborts.
- `send_dominant`: Drives TX low (`set pins, 0 [2]`).
- `jmp x--, bit_loop`: Loops over all 11 identifier bits.
- `irq 0` / `push noblock`: On winning entire identifier, raises `IRQ 0` and signals host CPU.

## 4. Cycle/Timing Analysis
- **Bit Period**: 5 to 7 cycles per bit depending on dominant/recessive path.
- **Collision Detection Latency**: Detected within 3 cycles of recessive bit assertion.
- **Total Frame Latency**: 82 cycles for 11-bit identifier transmission.

## 5. Verification Results
- **Uncontested Frame Transmission**: PASS. Completed 11 bits, asserted `IRQ 0`, zero collisions.
- **Contested Arbitration Collision**: PASS. Detected higher-priority node driving dominant state at cycle 16; asserted `IRQ 1` at cycle 23 and ceased transmission.
- **IMEM Occupancy**: 17/32 instructions (53.12%).

## 6. RFC: ISA & Hardware Recommendations
- **RFC-CAN-1: Hardware Dominant/Recessive Collision Trap in PAU**:
  - *Observation*: Software bitwise collision checking requires 4-5 instructions per bit, imposing an upper limit on maximum baud rate.
  - *Recommendation*: Add an `AUTOCAN` mode to `PAU_CFG`. When enabled, whenever an `out pins, 1` instruction transmits '1', hardware automatically samples the selected RX pin; if sampled as '0', hardware immediately forces TX high, halts execution, and asserts a hardware trap IRQ without software branching overhead.
