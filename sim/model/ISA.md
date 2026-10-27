# openPIO ISA v2

Each state machine (SM) executes 16-bit instructions from its own 64-word instruction memory,
one instruction per strobe: CLKDIV (host, HOST.md) makes every Nth clock a strobe. The chip has an even number of SMs sharing the pins
(see Multiple state machines); the host interface is in HOST.md. Encoding rule: codes of similar
or opposite operations differ only in their least significant bit.

```
 15  14  13 | 12  11  10   9   8 |  7   6   5   4   3   2   1   0
   opcode   |  delay / side-set  |            arguments
```

## Opcodes

| Opcode | Instruction | Pair |
|---|---|---|
| `000` | JMP, target 0..31 | JMP: bit 13 is target bit 5 |
| `001` | JMP, target 32..63 | |
| `010` | IN | IN / OUT: opposite |
| `011` | OUT | |
| `100` | ALU, operations 0..3 | ALU: bit 13 is operation bit 2 |
| `101` | ALU, operations 4..7 | |
| `110` | IRQ, PUSH / PULL, WAIT, PAU action | control / constants |
| `111` | SET | |

Reserved encodings execute as a NOP: delay and side-set apply, nothing else changes.

## Delay / side-set field [12:8]

Shared by every instruction. With `side_set_count` = n (0..5) and `side_set_opt`, the top
n (+1 if opt) bits are side-set and the rest is the delay:

- **Side-set**: the n-bit value is written to pins `sideset_base`.. (or to their pindirs with
  `side_set_pindirs`) when the instruction issues, before it executes. With opt, the field's
  MSB enables it.
- **Delay**: after the instruction completes (after its stall, if it stalls), the SM idles
  `delay x DLYMUL` strobes (DLYMUL: SET `100`, 1 after reset).

## Operand codes

Shared by IN (source), OUT (destination) and ALU (source, destination). `-` is reserved: reads 0,
a write is discarded.

| Code | Operand | IN src | OUT dest | ALU src | ALU dest |
|---|---|---|---|---|---|
| `000` | PINS | pins from `in_base`, or the PAU RX | pins from `out_base`, or the PAU TX | `gpio_in` | pin values |
| `001` | PINDIRS | - | pindirs from `out_base` | pindirs | pindirs |
| `010` | X | X | X | X | X |
| `011` | Y | Y | Y | Y | Y |
| `100` | ISR | ISR | ISR (count = bits) | ISR | ISR (count = 16) |
| `101` | OSR | OSR | - | OSR | OSR (count = 0) |
| `110` | NULL | zeros | discard | 0 | discard |
| `111` | special | STATUS | PC (indirect jump) | STATUS | COND |

ALU PINS / PINDIRS are all 8 pins, bits [7:0] (no base); the upper bits read 0 and are not
written. STATUS (read-only) is the SM status in bits [7:0] and the PAU status in bits [15:8],
indices 0..7 of the Conditions tables, bit i = index i.

## JMP `00A`

```
 0   0   A | delay / side-set | C2  C1  C0 | A4  A3  A2  A1  A0
```

Target = `{A, A4..A0}` (0..63). The condition is evaluated on the state before the instruction.

| Code | Condition | Jumps when | Pair |
|---|---|---|---|
| `000` | always | always | always / never |
| `001` | never | never (a NOP that only applies delay / side-set) | |
| `010` | X-- | X != 0, then X = X - 1 | X-- / Y-- |
| `011` | Y-- | Y != 0, then Y = Y - 1 | |
| `100` | COND0 | programmable condition 0 is true | programmable, see Conditions |
| `101` | COND1 | programmable condition 1 is true | |
| `110` | COND2 | programmable condition 2 is true | |
| `111` | COND3 | programmable condition 3 is true | |

## IN `010` / OUT `011`

```
 0   1   D | delay / side-set | operand (3) | Auto | count (4)
```

`count` 1..15 bits, 0 = 16. Shift directions and thresholds: MISC (see Per-SM configuration).
In time order the bits of a `count`-bit value go from bit 0 up when shifting right, from bit
`count - 1` down when shifting left (this order applies to the PAU queues).

- **IN**: shifts `count` bits of the source into the ISR; `isr_count += count` (max 16).
  IN PINS reads the PAU RX queue instead of the pins while the PAU receiver is on (see PAU).
  - Auto (autopush): then, if `isr_count >= push threshold`, pushes the ISR to the RX FIFO
    (ISR and count cleared); on a full RX FIFO it stalls until there is room, then pushes.
- **OUT**: shifts `count` bits out of the OSR into the destination; `osr_count += count`
  (max 16). OUT PC jumps to the low 6 bits. OUT PINS feeds the PAU TX queue instead of the
  pins while the PAU transmitter is on.
  - Auto (autopull): if `osr_count >= pull threshold` when the OUT issues, it stalls until the
    TX FIFO has a word, loads it into the OSR (count cleared) and issues again on the next
    strobe. After the shift, if `osr_count >= pull threshold` and the TX FIFO has a word, the
    OSR is reloaded in the same strobe, so a fed stream runs without gaps.

## ALU `10O`

```
 1   0   O2 | delay / side-set | Dest (3) | O1  O0 | Src (3)
```

Dest and Src are operand codes, the operation is `{O2, O1, O0}`. `D` is the destination's
value (0 for COND).

| Op | Operation | Result | Pair |
|---|---|---|---|
| `000` | MOV | Src | copy: straight / bit-reversed |
| `001` | REV | Src bit-reversed (16 bits) | |
| `010` | AND | D & Src | |
| `011` | OR | D \| Src | |
| `100` | XOR | D ^ Src | `xnor d, null` = NOT d |
| `101` | XNOR | ~(D ^ Src) | |
| `110` | ADD | D + Src (16 bits, no carry) | |
| `111` | SUB | D - Src (16 bits, no borrow) | |

## Control `110`

```
 1   1   0 | delay / side-set | Type (2) | arguments (6)
```

| Type | Instruction | Arguments [5:0] | Pair |
|---|---|---|---|
| `00` | IRQ | Clear, Wait, Peer, Index (3) | flags / FIFOs |
| `01` | PUSH / PULL | Dir, Block, IfCond, Peer, `00` | |
| `10` | WAIT | Pol, Edge, T_EN, `0`, Slot (2) | wait / PAU |
| `11` | PAU action | Action (2), `0000` | |

**IRQ**: `Clear` = 1 clears the flag, 0 sets it; `Wait` = 1 then stalls until the flag is
cleared; `Peer` = 1 selects the peer flag bank.

**PUSH / PULL**: `Dir` = 0 PUSH (ISR to RX FIFO, ISR and count cleared), 1 PULL (TX FIFO to
OSR, count cleared). `Block` = 1 stalls on a full RX / empty TX FIFO. Non-blocking: PUSH on a
full FIFO drops the word (ISR still cleared); PULL on an empty FIFO copies X into the OSR.
`IfCond` = 1: PUSH only when `isr_count >= push threshold`, PULL only when
`osr_count >= pull threshold`. `Peer` = 1: PUSH writes the peer SM's TX FIFO instead of the own
RX FIFO, PULL reads the peer's RX FIFO instead of the own TX FIFO (full / empty refer to that
FIFO): two SMs pass data without the host.

**WAIT**: stalls until programmable condition `Slot` (see Conditions) matches: level
(`Edge` = 0) equal to `Pol`, or edge (`Edge` = 1) rising (`Pol` = 1) / falling (`Pol` = 0)
since the previous strobe. With `T_EN` the stall also ends after TIMEOUT_VAL strobes, setting
the timeout flag (cleared when a timed WAIT issues).

**PAU action** (see PAU): `00` CRC reset, `01` CRC append, `10` TX abort, `11` reserved.

## SET `111`

```
 1   1   1 | delay / side-set | Dest (3) | Value (5)
```

| Code | Destination | Effect | Pair |
|---|---|---|---|
| `000` | PINS | pins `set_base`.. (`set_count` bits) = Value | PINS / PINDIRS |
| `001` | PINDIRS | pindirs `set_base`.. = Value | |
| `010` | X | X = Value (zero-extended) | X / Y |
| `011` | Y | Y = Value | |
| `100` | DLYMUL | delay multiplier = Value (0 = 1); does not change the strobe rate | timing |
| `101` | TIMEOUT_VAL | WAIT timeout = Value strobes; clears the timeout flag | |
| `110` | PAU_BAUD | PAU half-bit period = Value strobes (0 = 32) | PAU |
| `111` | PAU_CTL | PAU control: [0] TX, [1] RX, [2] stuffing, [3] CRC, [4] monitor | |

## Conditions

JMP COND0..COND3 and WAIT read four programmable conditions. Each is 8 bits:

```
 7   | 6   5   4 | 3   2   1   0
 Inv |    Src    |     Index
```

The condition is the selected signal, inverted when `Inv` = 1.

| Src | Signal | Index | Pair |
|---|---|---|---|
| `000` | GPIO pin | pin 0..7 | pins |
| `001` | pin `in_base + Index` | 0..7 | |
| `010` | IRQ flag | 0..7 | flags |
| `011` | peer IRQ flag | 0..7 | |
| `100` | bit of X | 0..15 | registers |
| `101` | bit of Y | 0..15 | |
| `110` | SM status | below | status |
| `111` | PAU status | below | |

| Index | SM status | PAU status | Pair |
|---|---|---|---|
| 0 | X == 0 | TX idle: queue empty, last bit sent, pins released if TX is off | X / Y; TX / RX |
| 1 | Y == 0 | RX idle: no input edge for 2 bit times | |
| 2 | X == Y | TX queue has room for 16 bits | |
| 3 | timeout flag | RX queue not empty | |
| 4 | OSR empty (`osr_count >= pull threshold`) | CRC == CRC_RES | shift registers; CRC |
| 5 | ISR full (`isr_count >= push threshold`) | CRC == 0 | |
| 6 | TX FIFO empty | stuff error (sticky) | FIFOs; RX errors |
| 7 | RX FIFO full | RX overrun (sticky) | |
| 8 | 0 | SE0: pins `in_base`, `in_base + 1` both low | line states |
| 9 | 0 | SE1: both high | |
| 10 | 0 | bit error (sticky): a monitored TX bit read back different | |
| 11..15 | 0 | 0 | |

**Edges.** Each strobe the SM keeps the four condition values; a WAIT edge compares them with
the current ones. The first strobe after a slot is rewritten compares against the old
definition's value.

**Registers.** Each SM has COND_CFG (written by the host) and COND (read by JMP and WAIT),
4 x 8 bits each. Both are written one condition at a time with the same 16-bit word:

```
 15 .. 10 | 9   8  | 7 .. 0
 ignored  |  Slot  | condition
```

- The host writes COND_CFG. CTRL.RESET copies COND_CFG into COND; a host write alone does not
  change COND.
- The program writes COND with `ALU dest COND` (result computed with D = 0). The new condition
  applies from the next instruction.
- Chip reset values (COND_CFG and COND), the conditions of ISA v1:

| Slot | Reset value | Condition |
|---|---|---|
| COND0 | `0x60` | X == 0 (v1 `!x`) |
| COND1 | `0x61` | Y == 0 (v1 `!y`) |
| COND2 | `0x00` | GPIO pin 0 (v1 `pin` with `jmp_pin` = 0) |
| COND3 | `0xE4` | not OSR empty (v1 `!osre`) |

## PAU (Protocol Acceleration Unit)

A serial transmitter and receiver between OUT / IN PINS and the pins, with its own bit timing,
line coding, bit stuffing and CRC. The program feeds and drains bits; the PAU keeps the line
timing exact.

```
 OUT PINS -> TX queue (32 bits) -> stuffing -> line encoder -> pin out_base (+1)
 IN PINS  <- RX queue (32 bits) <- destuffing <- decoder, clock recovery <- pin in_base (+1)
                 CRC: data bits, before stuffing / after destuffing
```

### Configuration

Protocol parameters, written by the host (MODE also gives the reset value of PAU_BAUD):

| Register | Field | Meaning |
|---|---|---|
| PAU_MODE | [1:0] ENC | line code: `00` NRZ, `01` NRZI, `10` Manchester, `11` biphase mark |
| | [2] INV | invert the line (TX pin and RX input) |
| | [3] DIFF | TX also drives `out_base + 1` with the complement; RX treats SE0 as the end of a frame |
| | [7:4] STUFF_LEN | run length before a stuffed bit, 1..15 (0 = 16) |
| | [9:8] STUFF_MODE | `00` after LEN 1s insert 0, `01` after LEN 0s insert 1, `10` after LEN equal bits insert the complement, `11` reserved |
| | [14:10] BAUD | PAU_BAUD after CTRL.RESET |
| CRC_CFG | [4:0] | CRC width - 1 (1..32 bits) |
| | [5] | initial value all ones (0: all zeros) |
| | [6] | CRC append sends the complement |
| CRC_POLY | 32 bits | polynomial, without the x^width term (two 16-bit registers) |
| CRC_RES | 32 bits | value of a correct received frame, for the CRC == CRC_RES status |

The program controls the PAU with SET PAU_BAUD, SET PAU_CTL and the PAU actions:

- **PAU_BAUD** = H: a half-bit lasts H strobes, a bit 2H. A new value applies from the next
  half-bit.
- **PAU_CTL**:
  - [0] TX on: OUT PINS feeds the TX queue and the PAU drives pin `out_base` (and
    `out_base + 1` with DIFF) instead of `gpio_out`. Turning it on starts the encoder from the
    pin's current level. Turning it off drains: the queued bits are still sent and the pins
    return to `gpio_out` at the end of the last one (turning it on again before that continues
    the stream).
  - [1] RX on: turning it on clears the RX queue, the decoder and the sticky errors, and arms
    the decoder for the first input edge.
  - [2] stuffing: TX bits queued while it is on are stuffed (a stuffed bit owed after the last
    of them is still sent); RX removes stuffed bits while it is on.
  - [3] CRC: feed the CRC with the data bits of TX (when TX is on) or else of RX.
  - [4] monitor: TX bits queued while it is on (and the stuffed bits after them) are compared,
    at the end of their first half, with the receive pin `in_base` (INV applied). On a
    difference the PAU sets the sticky bit error status and goes quiet: the pins return to
    `gpio_out` at once and the queued bits, and any bit queued while the error is set, are
    discarded (OUT PINS and CRC append still go to the PAU, not to the pins). Turning TX on
    clears the error. Wired-AND buses (CAN, LIN, 1-Wire) use it to stop at the first bit another
    node overrides, e.g. a lost arbitration, and check the status at the end of the frame.
- **CRC reset**: CRC = initial value. **CRC append**: queues the CRC (width bits, MSB first,
  complemented if configured) to TX; while the queue has no room it issues again every strobe.
  Appended bits do not feed the CRC and are stuffed like data.
- **TX abort**: discards the TX queue and the bit in progress; the pins return to `gpio_out`
  now and TX is off.

### Transmitter

OUT PINS with TX on appends its `count` bits to the TX queue in time order; while the queue has
no room for them it does nothing and issues again every strobe. With CRC on, the bits update the CRC as they are queued.
Every bit time the transmitter takes the next queued bit, or a stuffed bit, and drives it:

| ENC | Line during a bit b (before INV) |
|---|---|
| NRZ | b for the whole bit |
| NRZI | toggles at the start of the bit when b = 0, holds when b = 1 |
| Manchester | ~b for the first half, b for the second (IEEE 802.3: 1 = rising edge) |
| Biphase mark | toggles at the start of every bit, and in the middle when b = 1 |

With an empty queue the line holds its level; the next bit starts on the next half-bit.

Stuffing counts the stuffed-while-queued data bits sent (and stuffed bits, in mode `10`); after
STUFF_LEN qualifying bits the transmitter sends one stuffed bit, in its own bit time, and
restarts the count. A bit queued with stuffing off restarts the count. USB: NRZI, mode `00`, LEN 6. CAN: NRZ, mode `10`, LEN 5. HDLC: NRZI, mode `00`, LEN 5.

### Receiver

With RX on the receiver samples pin `in_base` (synchronized, INV applied) every strobe. Times
below are in strobes since the reference edge; H = PAU_BAUD.

| ENC | Bit recovery |
|---|---|
| NRZ, NRZI | every input edge restarts the bit timer; the line is sampled H after it and then every 2H. NRZ bit = sample; NRZI bit = 1 when the sample equals the previous one (the first is compared with the level at RX on). |
| Manchester | the first edge after RX on is a mid-bit edge. Each mid-bit edge gives a bit: rising = 1. Edges before 1.5H are bit boundaries and ignored; the next mid-bit edge must come by 2.5H, else the decoder re-arms for a new first edge. |
| Biphase mark | the first edge after RX on is a bit boundary. An edge between 0.5H and 1.5H is a mid-bit edge (bit = 1); the next boundary edge, between 1.5H and 2.5H, ends the bit (bit = 0 without a mid-bit edge). No boundary by 2.5H also ends it (the last bit of a frame) and re-arms the decoder. |

With DIFF, SE0 (pins `in_base` and `in_base + 1` both low, as a USB end of packet) decodes no
bit: while it lasts the decoder is re-armed for the first edge of the next frame.

Destuffing (stuffing on) drops the bit that follows STUFF_LEN qualifying bits; when that bit is
not the expected stuffed value the stuff error flag is set and the bit is kept. NRZ and NRZI
keep sampling an idle line: the program frames the data, e.g. it checks the CRC status right
after taking the last bit of the frame. Bits then feed
the CRC (CRC on, TX off) and enter the RX queue; a bit arriving at a full queue is dropped and
sets RX overrun. IN PINS with RX on takes `count` bits from the queue (oldest first in time order); while fewer
are queued it does nothing and issues again every strobe.

## Multiple state machines

- **Peer**: SM i and SM i XOR 1 are peers (0 and 1, 2 and 3). Peer IRQ flags are the peer's own
  flags: `irq peer 3` sets flag 3 of the peer, a peer IRQ condition reads the peer's flags.
  PUSH / PULL `Peer` reach the peer's FIFOs.
- **Pins**: the SMs share the 8 pins: one output latch and one direction latch. An SM changes
  only the pins in its PIN_MASK; when several SMs change the same pin in the same strobe, the
  highest-numbered SM wins. The PAU TX of an SM drives its pins over the latch, also within
  PIN_MASK and with the same priority. An instruction sees the latches as they were at the
  start of the clock, with the writes it made itself (side-set) applied.
- **Open drain** (chip, HOST.md GPIO): a pin in the open-drain mask never drives high: it is
  pulled low when its direction bit is 1 and its latch bit 0, and floats otherwise. Writing 1 to
  the latch releases the line, 0 pulls it low once the program has enabled the pin.
- **Start**: the host enables, resets or single-steps any set of SMs with one write (HOST.md
  CTRL), so SMs can start on the same clock.

## Per-SM configuration (host)

Independent of the instruction stream, written over SPI (HOST.md): wrap, side-set, pin bases,
PIN_MASK, MISC (shift directions, push / pull thresholds, 0 = 16), CLKDIV, COND_CFG, PAU_MODE,
CRC_CFG, CRC_POLY, CRC_RES. With the SM stopped the host can also write PC, X, Y, ISR, OSR and
the shift counts.

## Constraints

- **IMEM writes require `CTRL.EN = 0`.** The host must clear `CTRL.EN` before rewriting any IMEM
  word and may set it again only after the last write. The hardware fetches the next instruction
  one clock ahead (synchronous IMEM read), so a word rewritten while the state machine runs can
  execute once with its old contents, whereas `emu.py` always reads the current memory.
  Behaviour of IMEM writes with `CTRL.EN = 1` is undefined.
- **PAU receiver timing needs H >= 2** for Manchester and biphase mark (edge windows of H / 2).

## Changes from ISA v1

- 64-word IMEM, 6-bit PC, wrap and OUT PC; JMP on opcodes `000` / `001`.
- ALU on opcodes `100` / `101` with 8 operations (v1: MOV, XOR, ADD, REV on `101`).
- Control opcode `110`: IRQ (v1 `110`, arguments moved down by two bits), PUSH / PULL (v1 `100`),
  WAIT (v1 `001`, now on a programmable condition: GPIO pin, `in_base` pin, IRQ and peer IRQ are
  condition sources), PAU actions.
- Operand codes: PINDIRS `110` -> `001`, X `001` -> `010`, Y `010` -> `011`, NULL `011` -> `110`,
  OUT PC and ALU COND on `111`; ALU PINS / PINDIRS as source and destination; ALU source
  STATUS (always 0 in v1) removed.
- IN / OUT bit 4: autopush / autopull; MISC push / pull thresholds (v1: fixed 16), also used by
  IfCond and the OSR empty / ISR full conditions.
- SET: PINDIRS `110` -> `001`, X / Y as operands, DLYMUL on `100` (v1 SET CLKDIV `011` changed the
  strobe divider and scaled delays at once), TIMEOUT_VAL `100` -> `101`,
  PAU_BAUD `101` -> `110` (now a half-bit period), PAU_CFG -> PAU_CTL.
- JMP conditions: `!x`, `!y`, `timeout`, `pin`, `!osre` become programmable (COND0..3, reset
  values as v1 except `timeout`); `never` added; Y-- `100` -> `011`. The MISC `jmp_pin` field is
  replaced by COND_CFG slot 2.
- PAU v2: own bit timing, TX and RX, NRZ / NRZI / Manchester / biphase mark, programmable
  stuffing and CRC (up to 32 bits; parity is a 1-bit CRC with polynomial 1). v1 had TX-only
  NRZI with a stuffing toggle that took a data bit's time slot (a defect) and a Manchester TX
  queue; IRQ no longer waits for the PAU (PAU_BUSY): WAIT on PAU status TX idle instead.
- Delays count strobes times DLYMUL; CLKDIV only divides the strobe rate (v1: delays were scaled
  by CLKDIV too, except after a WAIT / PUSH / PULL stall).
- After CTRL.RESET the OSR is empty (`osr_count` = 16; v1: 0, a full OSR of zeros).
- STATUS as IN / ALU source `111`; PUSH / PULL `Peer`; several SMs with shared pins, PIN_MASK
  and chip-level open drain (v1: one SM, open drain rewrote its pindirs).
