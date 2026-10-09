# openPIO on the Sipeed Tang Nano 20K

`tang_top.v` wraps the unmodified Tiny Tapeout design (`tt_um_theotacconi_openpio`) for the Tang Nano 20K (GW2AR-LV18QN88C8/I7). The board runs it from the 27 MHz crystal directly, slower than the 50 MHz TT target.

## Wiring

Leave the RGB LCD panel unplugged: the `pio` pins share the 40-pin LCD connector.

| Signal | FPGA pin | Header | Direction | Notes |
|---|---|---|---|---|
| SPI SCLK | 73 | J6 | in | mode 0, <= 2.25 MHz (27 MHz / 12), pulled down |
| SPI MOSI | 74 | J6 | in | pulled down |
| SPI CS_n | 76 | J6 | in | pulled up (idle) |
| SPI MISO | 75 | J5 | out | |
| HOST_IRQ | 86 | J5 | out | high while any local IRQ flag is set |
| pio[0..4] | 27, 28, 29, 30, 31 | J6 | in/out | `uio[0..4]`, weak pull-ups |
| pio[5..7] | 41, 42, 48 | J5 | in/out | `uio[5..7]`, weak pull-ups |
| GND | | J5/J6 | | common ground with the SPI host |

All I/O is 3.3 V LVCMOS. The internal pull-ups are weak; add 4.7 kOhm pull-ups for I2C, 1-Wire or SWD.

Header and pin sources: Sipeed TangNano-20K-example `.cst` files and litex-boards `sipeed_tang_nano_20k.py`. The pins avoid the SD card (80-85), the MAX98357A amplifier (51, 54-56), HDMI DDC (52-53) and 1.8 V pins 25-26. Free pins left: 49, 71, 72.

## Buttons and LEDs

- **S1** (pin 88) resets the design while held. It reads 1 when pressed, so `tang_top` inverts it. A 16-cycle power-on reset also runs after configuration.

| LED | Lit when |
|---|---|
| 0 | HOST_IRQ |
| 1 | SPI frame in progress (stretched to 155 ms) |
| 2 | Activity on any `pio` pin (stretched) |
| 3 | The state machine drives at least one `pio` pin |
| 4 | Reset active |
| 5 | Heartbeat, about 0.8 Hz: the clock is running |

FIFO and stall flags are not on the TT pins; read STATUS (register 8) over SPI.

## Build

Open-source flow (oss-cad-suite, or `pip install yowasp-yosys yowasp-nextpnr-himbaechel-gowin apycula`):

```
./build.sh            # -> build/tang_top.fs (prints Fmax; ~31-34 MHz, passes 27 MHz)
./build.sh load       # program SRAM via the on-board BL616 (openFPGALoader -b tangnano20k)
./build.sh flash      # program the SPI flash
YOSYS=yowasp-yosys NEXTPNR=yowasp-nextpnr-himbaechel-gowin ./build.sh   # YoWASP tools
```

Gowin EDA: `gw_sh build_gowin.tcl` (uses `tang_nano_20k.cst` and `tang_nano_20k.sdc`; output `impl/pnr/openpio_tang.fs`).

## Bring-up

Use the SPI sequence in `docs/info.md` ("How to test"), with SCLK at or below 2.25 MHz. The echo program there is a quick end-to-end check: a word pushed with TX_PUSH comes back from RX_POP.
