# Captures

Logic analyzer recordings of the Tang Nano 20K build, 8x4 RTL (c2798d7), recorded on
2026-11-07 with `sigrok-cli` and a Saleae Logic clone (`fx2lafw`, 8 channels). Each one is a
sigrok session file, as PulseView saves it. `test/learn/test_captures.py` checks what the
learner finds in each one, without a board.

The chip's clock is 27 MHz. The bridge is wired as in [README.md](../README.md), and pio 0 is
FPGA pin 27, pio 1 pin 28.

| File | Line | Analyzer | What the chip did | Learned |
|---|---|---|---|---|
| `uart_115200.sr` | pio 0 | D0, 4 MHz, 2.5 s | `uart_tx.asm` on SM 0, CLKDIV 29 + 76/256, `openPIO says hello\r\n` 10 times | async serial 8N1, 234.40 clocks a bit (115200 baud) |
| `hdlc_9600.sr` | pio 0 | D0, 1 MHz | `hdlc_tx.asm` on SM 0 and `hdlc_rx.asm` on SM 1, the same pin, CLKDIV 351 + 144/256; six frames, all received with the right CRC | NRZI, 1406.38 clocks a half-bit, stuffing after 5 ones, sync 01111110, CRC-16 0x1021, init ones, xorout ones |
| `bridge_spi.sr` | the Pico's SPI | D0 SCLK (GP2), D1 MOSI (GP3), D2 CS_N (GP5), 12 MHz, 0.2 s | `bringup.py`, all ok; 55 transactions at 1 MHz | data D1 on clock D0, mode 0, 12 samples a bit, frames while D2 is 0 |
| `nec_remote.sr` | KY-022 output on pio 0 | D0, 1 MHz, 30 s | `nec_rx.asm` on SM 0, CLKDIV 27: ten keys, address 0, commands 45 46 47 16 0d 1c 08 5a 19 1c, a repeat code after each and more on the key held | pulse distance, idle high, leader 9.06 ms + 4.39 ms, spaces 541 / 1635 us, CRC-8 0x01 (each byte's complement); 13 repeat codes ignored |
| `ws2812_random.sr` | pio 1, the ring's DI before the 330 ohm | D0, 24 MHz, 1 s | `ws2812.asm` on SM 0, CLKDIV 3 + 96/256, 12 LEDs of random colours (`random.Random(5)`, each 0..23) every 40 ms; sigrok's WS281x decoder reads the colours sent | pulse width, idle low, marks of 9 / 21 samples (375 / 875 ns), a long one a 1, no CRC |

## Commands

The UART:

```
python3 board/listen.py board/captures/uart_115200.sr --channel D0 --clock-hz 27e6 --program
```

The HDLC frames:

```
python3 board/listen.py board/captures/hdlc_9600.sr --channel D0 --kind frames --program
```

The bridge's SPI:

```
python3 board/listen.py board/captures/bridge_spi.sr --channel D1 --clock-channel D0 \
    --select-channel D2 --select-pin 2 --clock-hz 12e6 --program
```

The remote. Without `--program`, the learned receiver is loaded on the board. On 2026-11-07 it
received keys 45 46 47 40 16, all with the right complements.

```
python3 board/listen.py board/captures/nec_remote.sr --channel D0 --pin 0 --lsb --program
```

The ring. Random colours, because frames that differ little do not give enough evidence. A
comet going round (DEMO) gave a CRC-15 that fit those frames by chance.

```
python3 board/listen.py board/captures/ws2812_random.sr --channel D0 --clock-hz 24e6 --program
```
