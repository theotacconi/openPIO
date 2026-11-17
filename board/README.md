# The chip on a board

A Raspberry Pi Pico (RP2040 or RP2350) is the SPI master: the PC sends it one line per SPI
transaction over USB and it answers with the MISO words. On the PC, `Board` has the same SPI
method as the model's `Chip`, so `sim/model/host.py` and the assembler drive the hardware
unchanged:

```python
from board import Board, Serial
from host import Host
from asm import assemble

host = Host(Board(Serial("/dev/ttyACM0")))
host.load(0, assemble(open("programs/uart/uart_tx.asm").read()))
host.start(0b01)
host.push(0, b"hello")
```

- `pico/main.py`: the Pico firmware, MicroPython. Its protocol is in its docstring.
- `board.py`: `Serial` (the Pico's USB port, standard library only, Linux and macOS), `Board`,
  and `Model`, the same bridge over the Python model for running without hardware.
- `bringup.py`: the first checks of a board: ID and pads, every MOSI and MISO bit, program
  memory, HOST_IRQ, 100 words through the FIFOs, and a UART on pin 0 read back by the other SM
  from the same pad. Only the bridge's wires are needed.
- `loopback.py`: the protocol programs through the chip's own pads, each line from one SM to the
  other (below).
- `listen.py`: from a logic analyzer's recording to a receiver on the board (below).
- `peripherals.py`: the UART, SPI and I2C programs against the Pico's own peripherals (below).
- `test_board.py`: the three on the model, without hardware.
- `demo.py`: the demo, a step at a time: a comet on a WS2812 ring, an infrared remote recorded,
  learned and received by the program the learner wrote (needs the ring on pin 1, an IR
  receiver on pin 0 and a logic analyzer; `--recording` without one). Every step of the
  learning is printed, and the program and the RX FIFO words read from the chip; all of it also
  goes to `demo-<date>-<time>.log`, each line with the time of day, to publish with a video.
- `explain.py`: what the learner saw and did with a recording, step by step (the widths
  measured, the leader, each frame's bits and time, the check, the timing, the machine code);
  no board needed: `python3 board/explain.py board/captures/nec_remote.sr`.
- `captures/`: logic analyzer recordings of the Tang Nano build, what the learner finds in each
  (`test/learn/test_captures.py`).

## Wiring: Pico to Tang Nano 20K

Step by step, with drawings: [fpga/tang_nano_20k/WIRING.md](../fpga/tang_nano_20k/WIRING.md).

| Signal | Pico GPIO | Pico pin | FPGA pin | Header |
|---|---|---|---|---|
| SCLK | GP2 | 4 | 73 | J6 |
| MOSI | GP3 | 5 | 74 | J6 |
| MISO | GP4 | 6 | 75 | J5 |
| CS_N | GP5 | 7 | 76 | J6 |
| HOST_IRQ | GP6 | 9 | 86 | J5 |
| GND | GND | 3 or 8 | GND | J5 / J6 |

Both boards are 3.3 V and each takes power from its own USB cable: connect only the signals
and ground. Other pins: edit the constants at the top of `pico/main.py`.

## Setup

1. MicroPython on the Pico: hold BOOTSEL while plugging it in and copy the UF2 from
   micropython.org for your board (RPI_PICO, RPI_PICO2, or the W ones: RPI_PICO_W,
   RPI_PICO2_W) to the drive that appears.
2. The bridge: `mpremote cp board/pico/main.py :main.py + reset` (`pipx install mpremote`).
   It starts at every power-up.
3. The FPGA: `fpga/tang_nano_20k/build.sh load`, or `flash` to keep it across power cycles.
   `openFPGALoader` talks to the board's USB, so it runs on the PC, not in the container.
4. `python3 board/bringup.py` (the Pico's port, `/dev/ttyACM0` by default; your user needs
   access to it, on Fedora the `dialout` group).

```
$ python3 board/bringup.py
ok    id
ok    lines
ok    imem
ok    irq
ok    echo
ok    uart
```

`python3 board/bringup.py --model` runs the same checks on the model. SCLK defaults to 1 MHz;
the chip takes up to `clk` / 6 (`--hz`): 4.5 MHz on the FPGA's 27 MHz, 8.3 MHz on the chip.

## The programs on the board

`loopback.py` runs the protocol programs on the board with no wires but the bridge's: a
transmitter on SM 0 and its receiver (or peer) on SM 1, on the same pads, and the host checks
what comes back: UART at 3.375 Mbaud and 115200, autobaud, full duplex on both SMs crossed,
HDLC with its FCS, USB low speed with its CRC16, CAN on one open-drain pad (a wired-AND bus of
two nodes, the pad's pull-up recessive, each frame acknowledged), 4b5b on BMC, SPI master to
target, SD commands with their CRC-7, I2S in lockstep, edge timestamps of a UART read back
as the learner reads a recording, and 10BASE-T framing with its FCS on pins 2 and 3 (4.5 Mbit/s
on the 27 MHz board: frames up to 450 bytes, which fit only with the programs' FIFO_SPLIT). A
logic analyzer on the pio pins sees every line. The checks on pins 0 and 1 fail while the demo's
infrared receiver (it drives pin 0) and ring are wired.

```
$ python3 board/loopback.py
ok    uart
ok    autobaud
...
ok    edges
```

Through the bridge an SPI word takes 16 us or more, longer than a byte of HDLC at 844 kbit/s or
of USB: those checks queue a frame whole with SM 0 stopped, as a host does for a line faster
than it can feed. `--model` runs the same checks on the model.

## Against the Pico's own peripherals

`loopback.py` checks each program against another program of this project, so a protocol
misread the same way by both would pass. `peripherals.py` checks them against the Pico's
UART, SPI and I2C, hardware written by Raspberry Pi and driven by MicroPython (the commands
are in `pico/main.py`):

- `uart_out`: `uart_tx` into the Pico's UART1, `uart_in`: the Pico's UART1 into `uart_rx`;
  128 bytes at 9600, 115200, 1 M and 3 M baud.
- `spi`: the Pico's SPI1 controller, mode 0, into `spi_target`: transfers of 1 to 32 bytes
  at 250, 500 and 900 kHz, the answers from the TX FIFO in order.
- `i2c`: `i2c_master` (open drain) against the Pico's I2C1 target, a memory of 256 bytes
  (`machine.I2CTarget`, MicroPython 1.26 or later): bytes written, read back with a repeated
  START, an absent address NACKed; at 100 and 400 kHz.

| Signal | Pico GPIO | Pico pin | | pio | FPGA pin | The chip's program |
|---|---|---|---|---|---|---|
| SPI1 SCK | GP10 | 14 | -> | 0 | 27 | `spi_target` |
| SPI1 MOSI | GP11 | 15 | -> | 1 | 28 | `spi_target` |
| CS_N | GP13 | 17 | -> | 2 | 29 | `spi_target` |
| SPI1 MISO | GP12 | 16 | <- | 3 | 30 | `spi_target` |
| UART1 RX | GP9 | 12 | <- | 4 | 31 | `uart_tx` |
| UART1 TX | GP8 | 11 | -> | 5 | 41 | `uart_rx` |
| I2C1 SCL | GP15 | 20 | <-> | 6 | 42 | `i2c_master` |
| I2C1 SDA | GP14 | 19 | <-> | 7 | 48 | `i2c_master` |

SCL and SDA each need a pull-up of about 5 kohm to the Pico's 3V3(OUT), pin 36. After the
checks the Pico's pins are inputs again; `loopback.py` and `bringup.py` drive pins 0-5
themselves, so run them with the Pico's peripherals off (after `peripherals.py`, or after a
reset of the Pico).

`wires` comes first: each wire alone, both levels, the others at the opposite level, so a
wire in the wrong hole shows by name before any protocol does. With `--sigrok` a logic
analyzer, D0-D7 on pio 0-7, records the protocol checks and sigrok's decoders must read on
the wires what each of them sent: a third implementation of each protocol, beside the chip's
and the Pico's.

```
$ python3 board/peripherals.py --sigrok
the Pico: MicroPython 1.29.0 i2ctarget
ok    wires
ok    uart_out
ok    uart_in
ok    spi
ok    i2c
ok    sigrok    the decoders read all 17 on the wires
```

## From a recording to a receiver

`listen.py` takes a recording of a line, finds its format with
[sim/learn/learn.py](../sim/learn/learn.py), writes an openPIO receiver for it, loads it on the
board and prints every frame it receives:

1. Record the line with a logic analyzer in PulseView (any sigrok-supported analyzer, such as
   the 8-channel 24 MHz ones) and save the session as a `.sr` file. A few frames are enough;
   a CRC is believed from about 40 bits of evidence, e.g. 4 frames with a CRC-16. One frame
   the line corrupted in the recording is spared: the CRC is learned from the others.
2. Connect the line to a `pio` pin of the board (and the analyzer's ground to the board's).
3. Run listen.py with the recording's channel and the board's pin:
   `python3 board/listen.py remote.sr --channel D0 --pin 0 --lsb`.

A NEC remote's recording, sampled at 1 MHz and played here on the model (`--model
--clock-hz 1e6`, which a board does not need), prints:

```
learned pulses: pulse distance, idle high, spaces of 562.5 / 1687.5 clocks (a long one is a 1), leader 9000 4500, sync -, no CRC
receiver on pin 0, CLKDIV 8 + 0/256 at 1 MHz
playing the recording
  32 bits  82 7d b7 48
  32 bits  0e f1 ee 11
  ...
```

Each line is a frame: the address, its complement, the command, its complement.

- `--kind` picks the family (pulses, frames, async, clocked); by default, of pulses and
  frames, the one that explains the recording with fewer frames, else async. `--kind async`
  also takes a line with breaks (LIN, DMX). A clocked line needs `--clock-channel` (and
  `--select-channel` / `--select-pin` if it has a select); its clock goes to pin `--pin` + 1.
- `--lsb` prints each byte's first bit as its LSB (UART, NEC, HDLC); the default is MSB first
  (SPI, CAN, DALI).
- `--clock-hz` is the board's clock, which the receiver counts: 27 MHz on the Tang Nano 20K,
  50 MHz on the chip.
- `--program` prints the receiver's source and stops; `--model` plays the recording on the
  Python model's pin instead of using the board (slow: keep the recording short).

The receiver runs on state machine 0 alone. SM 1 is left as it is, so a transmitter loaded
there on the same pin is received: the chip learns its own line.

Frames the recording cut at its start or end are left out of the learning. The receiver
learns the line only: request-and-answer protocols need the hand-written programs in
[programs/](../programs/).
