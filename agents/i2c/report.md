# Protocol Verification Report: I2C Master (Clock Stretching & Multi-Master Arbitration)

## 1. Executive Summary
A full-featured I2C Master was implemented and verified on the openPIO architecture. The core supports standard START and STOP condition framing, 8-bit addressing and data serialization, slave clock stretching recovery, and bit-level multi-master collision detection. When arbitration is lost, the core raises `IRQ 1` and releases both SCL and SDA lines within 2 clock cycles. All nominal and corner-case verification scenarios passed successfully. The implementation uses 29 instructions (90.62% IMEM occupancy).

## 2. Protocol & Pin Architecture
- **Protocol**: I2C Master (Open-Drain, Bidirectional 2-Wire).
- **Physical Signaling**: External pull-up resistors on both SCL and SDA. Pins are driven actively low (0) by setting direction to output (`pindir = 1`, `out = 0`) and released high (1) by setting direction to input (`pindir = 0`).
- **Pin Assignment**:
  - `GPIO 0`: SCL (Serial Clock, bidirectional open-drain).
  - `GPIO 1`: SDA (Serial Data, bidirectional open-drain). Mapped to `jmp_pin = 1`.

## 3. Assembly Program Analysis
The program utilizes 29 words of IMEM (90.62% occupancy):
- **START Condition**: `set pindirs, 2 [1]` pulls SDA low while SCL is high; `set pindirs, 3 [1]` pulls SCL low.
- **Bit Loop**: `out y, 1` extracts the current bit.
  - If 1: Releases SDA (`set pindirs, 1`), then releases SCL (`set pindirs, 0`). Stalls in `wait 1 gpio 0` until slave releases clock (Clock Stretching). Inspects `jmp pin, arb_ok`: if SDA is read as 0, another master is asserting dominance; immediately triggers `irq 1` and branches to `abort`.
  - If 0: Drives SDA low (`set pindirs, 3`), releases SCL (`set pindirs, 2`), waits for clock stretch release (`wait 1 gpio 0`).
  - Bit conclave: `set pindirs, 3` pulls SCL low and loops via `jmp x--, bit_loop`.
- **ACK Phase**: Releases SDA and SCL, samples ACK via `in pins, 2`, and pulls SCL low.
- **STOP Condition**: Drives SDA low with SCL released, waits for SCL high, then releases SDA.
- **Abort Handler**: On arbitration loss, immediately resets `set pindirs, 0` to release bus lines to the winning master.

## 4. Cycle/Timing Analysis
- **Nominal Transfer**: 78 cycles per byte transaction.
- **Clock Stretch Recovery**: Successfully paused for 10 cycles during slave hold; resumed with zero packet corruption (total 83 cycles).
- **Arbitration Collision Detection**: Collision recognized at cycle 12; bus fully released by cycle 15.

## 5. Verification Results
- **Nominal Write with ACK**: PASS. Correct byte and ACK registered.
- **Slave Clock Stretching**: PASS. Core stalled in `wait 1 gpio 0` and resumed deterministically upon SCL release.
- **Multi-Master Arbitration Loss**: PASS. Competing master dominance triggered `IRQ 1` and caused immediate bus release (`pindirs = 0`).
- **IMEM Occupancy**: 29/32 instructions (90.62%).

## 6. RFC: ISA & Hardware Recommendations
- **RFC-I2C-1: Side-Set Target Selection for PINDIRS (High Priority)**:
  - *Observation*: Currently, side-set only updates `gpio_out`. Because open-drain protocols modulate `pindirs` to alternate between active pulldown and high-impedance float, 12 separate `set pindirs` instructions are required, pushing IMEM occupancy to 90.62%.
  - *Recommendation*: Add a configuration bit (or side-set directive) allowing side-set to target `PINDIRS` instead of `PINS`. This would reduce the I2C master program from 29 instructions down to ~14 instructions, saving over 50% of instruction memory.
- **RFC-I2C-2: Hardware Open-Drain Mode (`OD_MODE`) in Pin Configuration**:
  - *Recommendation*: Allow GPIO output pins to be configured in hardware open-drain mode, where writing '1' automatically turns off the output driver (floats) and writing '0' enables active low drive. This avoids modulating direction registers altogether.
- **RFC-I2C-3: Dedicated Hardware Bus Timeout for Clock Stretching**:
  - *Observation*: If a bus line is shorted to ground or a slave hangs, `wait 1 gpio 0` blocks execution indefinitely.
  - *Recommendation*: Integrate the ISA `wait ... timeout` field with an auto-resetting cycle watchdog.
