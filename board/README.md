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
   micropython.org (RPI_PICO or RPI_PICO2) to the drive that appears.
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
