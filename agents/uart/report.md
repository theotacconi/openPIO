# Protocol Verification Report: UART with Hardware Framing Check

## 1. Executive Summary
The Universal Asynchronous Receiver-Transmitter (UART) receiver core was implemented and verified on the openPIO architecture. The program implements standard 8N1 serial reception at 8 cycles per bit period and incorporates a cycle-accurate hardware framing check that inspects the stop bit midpoint. If a framing error occurs (stop bit line sampled low instead of idle high), the core halts FIFO insertion and raises an asynchronous interrupt (`IRQ 0`). Both nominal reception and framing corruption scenarios passed with 100% compliance.

## 2. Protocol & Pin Architecture
- **Protocol**: UART Receiver (8N1: 1 Start bit, 8 Data bits LSB-first, 1 Stop bit).
- **Physical Signaling**: Asynchronous NRZ, idle-high line.
- **Pin Assignment**:
  - `GPIO 0`: Serial Input (RX). Mapped to `in_base = 0` and `jmp_pin = 0`.
- **Clocking & Timing**: 8 cycles per bit period. Center-aligned sampling.

## 3. Assembly Program Analysis
The assembly program (`uart.asm`) occupies 10 words out of the 32-word IMEM capacity (31.25% occupancy):
- `wait 0 gpio 0 [10]`: Stalls until start bit falling edge is detected. Upon edge detection, applies a 10-cycle delay plus instruction fetch cycles to align execution directly at cycle 12 (center of data bit 0).
- `set x, 7`: Loads 8-bit loop iteration count into register X.
- `in pins, 1 [6]`: Captures the serial bit from GPIO 0 into the ISR and delays 6 cycles.
- `jmp x--, bit_loop`: Decrements X and branches to sample the next bit (total bit loop latency = 1 + 6 + 1 = 8 cycles).
- `nop [7]`: Delays to land in the center of the stop bit.
- `jmp pin, stop_ok`: Tests `jmp_pin` (GPIO 0). If high (valid stop bit), branches to FIFO push.
- `irq 0` / `jmp wait_idle`: If stop bit is low (framing error), raises IRQ 0 and branches to wait for line recovery without pushing corrupted data to FIFO.
- `push noblock`: Transfers received 8-bit payload from ISR into RX FIFO.
- `wait 1 gpio 0`: Guarantees line returns to idle high before re-entering wait for next start bit.

## 4. Cycle/Timing Analysis
- **Bit Period**: 8 machine cycles.
- **Start Bit Center**: Cycle 4 (detected at 0, centered at 4).
- **First Data Bit Sample**: Cycle 12 (center of bit 0).
- **Subsequent Data Bit Samples**: Cycles 20, 28, 36, 44, 52, 60, 68.
- **Stop Bit Validation**: Sampled at cycle 76 (midpoint of stop bit).
- **Total Frame Latency**: 93 cycles per byte.

## 5. Verification Results
- **Valid 8N1 Frame (`0xA5`)**: PASS. Received payload matched `0xA5`, RX FIFO size = 1, `IRQ 0` remained low.
- **Framing Error Injection (`0x5A` corrupted stop bit = 0)**: PASS. `IRQ 0` asserted, corrupt byte rejected from FIFO.
- **IMEM Occupancy**: 10/32 instructions (31.25%).

## 6. RFC: ISA & Hardware Recommendations
- **RFC-UART-1: Hardware Auto-Baud Counter / Fractional Clock Divider**:
  - *Observation*: At higher baud rates or non-integer CPU clock ratios, software delay fields (`[0-31]`) cannot represent fractional dividers, resulting in accumulated phase drift on long frames.
  - *Recommendation*: Introduce an internal fractional clock prescaler or dedicated baud counter per state machine.
- **RFC-UART-2: Direct Conditional Branch on Sampled Pin State without Dedicated jmp_pin**:
  - *Observation*: Currently, conditional branching on an input pin requires pre-configuring `jmp_pin`. A general `jmp pin[N]` or testing an ISR bit would permit multi-channel monitoring in a single SM.
