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
- `listen.py`: from a logic analyzer's recording to a receiver on the board (below).
- `test_board.py`: both on the model, without hardware.

## Wiring: Pico to Tang Nano 20K

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

## From a recording to a receiver

`listen.py` takes a recording of a line, finds its format with
[sim/learn/learn.py](../sim/learn/learn.py), writes an openPIO receiver for it, loads it on the
board and prints every frame it receives:

1. Record the line with a logic analyzer in PulseView (any sigrok-supported analyzer, such as
   the 8-channel 24 MHz ones) and save the session as a `.sr` file. A few frames are enough;
   a CRC is believed from about 40 bits of evidence, e.g. 4 frames with a CRC-16.
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

- `--kind` picks the family (pulses, frames, async, clocked); by default the first of pulses,
  frames and async that explains the recording. A clocked line needs `--clock-channel` (and
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
