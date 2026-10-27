# openPIO ISA v2

One state machine (SM) executes 16-bit instructions from a 64-word instruction memory, one
instruction per strobe (see CLKDIV). Encoding rule: codes of similar or opposite operations
differ only in their least significant bit.

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
| `100` | reserved (NOP), to be assigned with PAU v2 | |
| `101` | IRQ, PUSH / PULL, WAIT | events and FIFOs |
| `110` | ALU | ALU / SET: write a register from a register / a constant |
| `111` | SET | |

Reserved encodings execute as a NOP: delay and side-set apply, nothing else changes.

## Delay / side-set field [12:8]

Shared by every instruction. With `side_set_count` = n (0..5) and `side_set_opt`, the top
n (+1 if opt) bits are side-set and the rest is the delay:

- **Side-set**: the n-bit value is written to pins `sideset_base`.. (or to their pindirs with
  `side_set_pindirs`) when the instruction issues, before it executes. With opt, the field's
  MSB enables it.
- **Delay**: after the instruction completes (after its stall, if it stalls), the SM idles
  `delay x CLKDIV` strobes.

## Operand codes

Shared by IN (source), OUT (destination) and ALU (source, destination). `-` is reserved: reads 0,
a write is discarded.

| Code | Operand | IN src | OUT dest | ALU src | ALU dest |
|---|---|---|---|---|---|
| `000` | PINS | pins from `in_base` | pins from `out_base`, through the PAU | `gpio_in` | `gpio_out` |
| `001` | PINDIRS | - | pindirs from `out_base` | pindirs | pindirs |
| `010` | X | X | X | X | X |
| `011` | Y | Y | Y | Y | Y |
| `100` | ISR | ISR | ISR (count = bits) | ISR | ISR (count = 16) |
| `101` | OSR | OSR | - | OSR | OSR (count = 0) |
| `110` | NULL | zeros | discard | 0 | discard |
| `111` | special | - | PC (indirect jump) | - | COND |

ALU PINS / PINDIRS are all 8 pins, bits [7:0] (no base); the upper bits read 0 and are not
written. Writes to `gpio_out` go through the open-drain mask, as every pin write.

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

- **IN**: shifts `count` bits of the source into the ISR; `isr_count += count` (max 16).
  - Auto (autopush): then, if `isr_count >= push threshold`, pushes the ISR to the RX FIFO
    (ISR and count cleared); on a full RX FIFO it stalls until there is room, then pushes.
- **OUT**: shifts `count` bits out of the OSR into the destination; `osr_count += count`
  (max 16). OUT PC jumps to the low 6 bits. OUT PINS goes through the PAU (see PAU).
  - Auto (autopull): if `osr_count >= pull threshold` when the OUT issues, it stalls until the
    TX FIFO has a word, loads it into the OSR (count cleared) and issues again on the next
    strobe. After the shift, if `osr_count >= pull threshold` and the TX FIFO has a word, the
    OSR is reloaded in the same strobe, so a fed stream runs without gaps.

## IRQ / PUSH / PULL / WAIT `101`

```
 1   0   1 | delay / side-set | Type (2) | arguments (6)
```

| Type | Instruction | Arguments [5:0] |
|---|---|---|
| `00` | IRQ | Clear, Wait, Peer, Index (3) |
| `01` | PUSH / PULL | Dir, Block, IfCond, `000` |
| `10` | WAIT | Pol, Edge, T_EN, `0`, Slot (2) |
| `11` | reserved (NOP) | |

**IRQ**: `Clear` = 1 clears the flag, 0 sets it; `Wait` = 1 then stalls until the flag is
cleared; `Peer` = 1 selects the peer flag bank. Setting a flag while the Manchester TX queue is
not empty first stalls until it drains (PAU_BUSY).

**PUSH / PULL**: `Dir` = 0 PUSH (ISR to RX FIFO, ISR and count cleared), 1 PULL (TX FIFO to
OSR, count cleared). `Block` = 1 stalls on a full RX / empty TX FIFO. Non-blocking: PUSH on a
full FIFO drops the word (ISR still cleared); PULL on an empty FIFO copies X into the OSR.
`IfCond` = 1: PUSH only when `isr_count >= push threshold`, PULL only when
`osr_count >= pull threshold`.

**WAIT**: stalls until programmable condition `Slot` (see Conditions) matches: level
(`Edge` = 0) equal to `Pol`, or edge (`Edge` = 1) rising (`Pol` = 1) / falling (`Pol` = 0)
since the previous strobe. With `T_EN` the stall also ends after TIMEOUT_VAL strobes, setting
the timeout flag (cleared when a timed WAIT issues).

## ALU `110`

```
 1   1   0 | delay / side-set | Dest (3) | Op (2) | Src (3)
```

Dest and Src are operand codes. `D` is the destination's value (0 for COND).

| Op | Operation | Result | Pair |
|---|---|---|---|
| `00` | MOV | Src | one operand: straight / reversed |
| `01` | REV | Src bit-reversed (16 bits) | |
| `10` | XOR | D ^ Src | two operands |
| `11` | ADD | D + Src (16 bits, no carry) | |

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
| `100` | CLKDIV | integer divider = Value (0, 1 = 1x) | timing |
| `101` | TIMEOUT_VAL | WAIT timeout = Value strobes; clears the timeout flag | |
| `110` | PAU_BAUD | PAU half-period, in strobes | PAU |
| `111` | PAU_CFG | PAU engines (see PAU) | |

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
| `110` | SM status | see below | status |
| `111` | PAU status | defined with PAU v2; reads 0 | |

| Index | SM status | Pair |
|---|---|---|
| 0 | X == 0 | X / Y |
| 1 | Y == 0 | |
| 2 | X == Y | |
| 3 | timeout flag | |
| 4 | OSR empty (`osr_count >= pull threshold`) | shift registers |
| 5 | ISR full (`isr_count >= push threshold`) | |
| 6 | TX FIFO empty | FIFOs |
| 7 | RX FIFO full | |
| 8..15 | 0 (reserved) | |

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
- The program writes COND with `ALU dest COND` (MOV / REV / XOR / ADD of the source with D = 0).
  The new condition applies from the next instruction.
- Chip reset values (COND_CFG and COND), the conditions of ISA v1:

| Slot | Reset value | Condition |
|---|---|---|
| COND0 | `0x60` | X == 0 (v1 `!x`) |
| COND1 | `0x61` | Y == 0 (v1 `!y`) |
| COND2 | `0x00` | GPIO pin 0 (v1 `pin` with `jmp_pin` = 0) |
| COND3 | `0xE4` | not OSR empty (v1 `!osre`) |

## PAU (Protocol Acceleration Unit)

To be revised in PAU v2. Current engines, selected by PAU_CFG on OUT PINS:

| PAU_CFG bit | Engine |
|---|---|
| 0 | NRZI encode on pins `out_base`, `out_base + 1` (J = 01, K = 10) |
| 1 | USB bit-stuffing (with bit 0): a toggle after six consecutive 1s |
| 2 | reserved (parity) |
| 3 | Manchester TX on pin `out_base`: each bit is PAU_BAUD strobes of b then of ~b |
| 4 | reserved (Manchester RX) |

Manchester TX has priority over NRZI. OUT PINS stalls (PAU_TX) while more than one symbol is
queued. SET PINS resets the NRZI state on J / K and flushes the Manchester queue, as does
SET PAU_CFG without bit 3. Known v1 defect: the stuffed toggle takes the time slot of the next
data bit instead of an extra bit time.

## Per-SM configuration (host)

Independent of the instruction stream, written over SPI (docs/info.md):

- wrap_bottom / wrap_top (6 bits each; after wrap_top the PC continues at wrap_bottom);
- side-set: count, opt, pindirs, base; in_base, out_base, set_base, set_count;
- MISC: ISR / OSR shift direction, push threshold and pull threshold (4 bits each, 0 = 16);
- open-drain mask; CLKDIV integer and fraction; COND_CFG.

## Constraints

- **PAU_BAUD during Manchester TX is undefined.** Writing `PAU_BAUD` while the Manchester TX
  engine (`PAU_CFG[3]`) still has samples queued for the pin is an illegal condition. The timing
  of the remaining queued half-periods is undefined: `emu.py` keeps the samples already expanded
  at the old baud, `src/pau.v` re-times them with the new value. Firmware must let the queue
  drain first (e.g. an `IRQ` set, which stalls until the queue is empty) before changing
  `PAU_BAUD`.
- **IMEM writes require `CTRL.EN = 0`.** The host must clear `CTRL.EN` before rewriting any IMEM
  word and may set it again only after the last write. The hardware fetches the next instruction
  one clock ahead (synchronous IMEM read), so a word rewritten while the state machine runs can
  execute once with its old contents, whereas `emu.py` always reads the current memory.
  Behaviour of IMEM writes with `CTRL.EN = 1` is undefined.

## Changes from ISA v1

- 64-word IMEM, 6-bit PC, wrap and OUT PC; JMP on opcodes `000` / `001`.
- WAIT `001` -> `101` Type `10`, on a programmable condition (v1: GPIO pin, `in_base` pin, IRQ,
  peer IRQ, all available as condition sources); PUSH / PULL `100` -> `101` Type `01`;
  IRQ `110` -> `101` Type `00` (arguments moved down by two bits); ALU `101` -> `110`;
  opcode `100` reserved.
- Operand codes: PINDIRS `110` -> `001`, X `001` -> `010`, Y `010` -> `011`, NULL `011` -> `110`,
  OUT PC and ALU COND on `111`; ALU PINS / PINDIRS as source and destination; ALU source
  STATUS (always 0 in v1) removed.
- IN / OUT bit 4: autopush / autopull; MISC push / pull thresholds (v1: fixed 16), also used by
  IfCond and the OSR empty / ISR full conditions.
- ALU operations: REV `11` -> `01`, XOR `01` -> `10`, ADD `10` -> `11`.
- SET: PINDIRS `110` -> `001`, X / Y as operands, CLKDIV `011` -> `100`, TIMEOUT_VAL `100` -> `101`,
  PAU_BAUD `101` -> `110`.
- JMP conditions: `!x`, `!y`, `timeout`, `pin`, `!osre` become programmable (COND0..3, reset
  values as v1 except `timeout`); `never` added; Y-- `100` -> `011`. The MISC `jmp_pin` field is
  replaced by COND_CFG slot 2.
- Delay always scaled by CLKDIV (v1: not after a WAIT / PUSH / PULL stall).
