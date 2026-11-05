#!/usr/bin/env python3
"""loopback.py - The protocol programs on a board, each line from one SM to the other through
the chip's own pads: a transmitter on SM 0, the receiver on SM 1 (or the peer program), and
what comes back checked by the host. Only the bridge's wires are needed; a logic analyzer on
the pio pins sees every line (pins in each check's docstring).

    python3 board/loopback.py                 # the Pico on /dev/ttyACM0
    python3 board/loopback.py uart can        # some checks only
    python3 board/loopback.py --model         # the same checks on the Python model

The rates are those of the board's 27 MHz clock (the chip's 50 MHz runs them faster).
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

from board import Board, Model, Serial

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "test" / "shared"))
from asm import assemble                                 # noqa: E402  (board put it on the path)
from chip import CTRL, FIFO_LEVEL, GPIO, IRQ             # noqa: E402
from devices import can_bits, crc7_sd                    # noqa: E402
from host import Host                                    # noqa: E402
from learn import AsyncSerial, Capture, characters       # noqa: E402

RELEASE = """
.program release
.pin_mask 0, 1, 2, 3, 4, 5, 6, 7
    mov pindirs, null
stop:
    jmp stop
"""


def program(path: str):
    return assemble((ROOT / "programs" / path).read_text())


def rx_level(host: Host, n: int) -> int:
    return host.read(n, FIFO_LEVEL)[0] >> 8


def wait(host: Host, done, polls: int = 5000) -> bool:
    """Poll until done() is true; every poll is a few SPI words of chip time."""
    return any(done() for _ in range(polls))


def wait_rx(host: Host, n: int, count: int, polls: int = 5000) -> bool:
    return wait(host, lambda: rx_level(host, n) >= count, polls)


def flags(host: Host, n: int) -> int:
    return host.read(n, IRQ)[0] & 0xFF


def clear_flags(host: Host, n: int) -> None:
    host.write(n, IRQ, 0x00FF)


def release(host: Host) -> None:
    """Every pio pin back to an input, open drain off: the pin latches outlive an SM reset."""
    host.write(0, CTRL, 0x30)
    host.load(0, assemble(RELEASE))
    host.start(0b01)
    host.write(0, CTRL, 0x30)
    host.write(0, GPIO, 0)


def differ(what: str, sent: list, got: list) -> str | None:
    if got == sent:
        return None
    k = next((i for i in range(min(len(sent), len(got))) if sent[i] != got[i]), min(len(sent), len(got)))
    return f"{what}: {len(got)} of {len(sent)} back, first difference at {k}"


def send_whole(host: Host, words: list[int]) -> None:
    """A frame into SM 0's TX FIFO with SM 0 stopped, then SM 0 run again: through the bridge an
    SPI word takes 16 us or more, longer than a byte of a fast line, and a frame the program
    runs out of mid-way is broken (the model's host is the chip's own SCLK, clk / 6)."""
    host.start(0b10)
    host.push(0, words)
    host.start(0b11)


def check_uart(host: Host) -> str | None:
    """uart_tx on pin 0 into uart_rx: 3.375 Mbaud (CLKDIV 1) and 115200 (29 + 76/256)."""
    for div, frac in ((1, 0), (29, 76)):
        host.write(0, CTRL, 0x30)
        host.load(0, program("uart/uart_tx.asm"), clkdiv_int=div, clkdiv_frac=frac)
        host.load(1, program("uart/uart_rx.asm"), clkdiv_int=div, clkdiv_frac=frac, in_base=0)
        host.start(0b11)
        data = [random.Random(div).randrange(256) for _ in range(64)]
        host.push(0, data)
        wait_rx(host, 1, len(data), 20000)
        error = differ(f"CLKDIV {div} + {frac}/256", data, [w >> 8 for w in host.pop(1, rx_level(host, 1))])
        if error or flags(host, 1) & 1:
            return error or f"CLKDIV {div}: framing error"
    return None


def check_autobaud(host: Host) -> str | None:
    """uart_autobaud (CLKDIV 4) learns 115200 from a leading 0x55 on pin 0."""
    host.load(0, program("uart/uart_tx.asm"), clkdiv_int=29, clkdiv_frac=76)
    host.load(1, program("uart/uart_autobaud.asm"), clkdiv_int=4, in_base=0)
    host.start(0b11)
    data = [random.Random(4).randrange(256) for _ in range(40)]
    host.push(0, [0x55] + data)
    wait_rx(host, 1, 1 + len(data), 20000)
    words = host.pop(1, rx_level(host, 1))
    if not words:
        return "nothing measured"
    if abs(words[0] - 8 * 234.375 / 4) > 2:
        return f"8 bit times measured as {words[0]} strobes, not 469"
    return differ("bytes", data, [w >> 8 for w in words[1:]]) or ("framing error" if flags(host, 1) & 1 else None)


def check_duplex(host: Host) -> str | None:
    """uart_duplex on both SMs, crossed: SM 0 sends on pin 0 and reads pin 1, SM 1 the other way
    round; both directions at once, CLKDIV 2 (1.69 Mbaud)."""
    host.load(0, program("uart/uart_duplex.asm"), clkdiv_int=2)
    host.load(1, program("uart/uart_duplex.asm"), clkdiv_int=2, out_base=1, set_base=1, in_base=0)
    rnd = random.Random(5)
    out = [[rnd.randrange(256) for _ in range(48)] for _ in range(2)]
    host.push(0, out[0])
    host.push(1, out[1])
    host.start(0b11)
    wait(host, lambda: rx_level(host, 0) >= 48 and rx_level(host, 1) >= 48, 20000)
    for n in (0, 1):
        error = differ(f"into SM {n}", out[1 - n], [w >> 8 for w in host.pop(n, rx_level(host, n))])
        if error or flags(host, n) & 1:
            return error or f"SM {n}: framing error"
    return None


def check_hdlc(host: Host) -> str | None:
    """hdlc_tx on pin 0 into hdlc_rx, CLKDIV 4 (844 kbit/s): 40 frames of 1 to 20 bytes, some
    with flag-like bytes, each after an idle line as long as the host takes, each queued whole."""
    host.load(0, program("hdlc/hdlc_tx.asm"), clkdiv_int=4)
    host.load(1, program("hdlc/hdlc_rx.asm"), clkdiv_int=4)
    host.start(0b11)
    rnd = random.Random(6)
    for k in range(40):
        frame = [rnd.choice((0x7E, 0x7D, 0xFF, rnd.randrange(256))) for _ in range(1 + k % 20)]
        send_whole(host, [len(frame), *frame])
        wait_rx(host, 1, len(frame) + 3)
        words = host.pop(1, rx_level(host, 1))
        if [w >> 8 for w in words[:-3]] != frame or not words or words[-1] != 0x7E03:
            return f"frame {k} of {len(frame)} bytes: {len(words)} words back, end {words[-1] if words else None}"
    return None


def check_usb(host: Host) -> str | None:
    """usb_tx on pins 4 (D-) and 5 (D+) into usb_rx, 1.5 Mbit/s (12 MHz strobes, CLKDIV 2.25):
    DATA0 / DATA1 packets of 0 to 8 bytes, runs of 1s stuffed, the CRC16 checked by usb_rx; each
    packet queued whole."""
    host.load(0, program("usb/usb_tx.asm"), clkdiv_int=2, clkdiv_frac=64, out_base=4, set_base=4)
    host.load(1, program("usb/usb_rx.asm"), clkdiv_int=2, clkdiv_frac=64, in_base=4)
    host.start(0b11)
    rnd = random.Random(7)
    for k in range(20):
        payload = [rnd.choice((0xFF, rnd.randrange(256))) for _ in range(k % 9)]
        pid = (0xC3, 0x4B)[k & 1]
        send_whole(host, [0x80 | pid << 8, len(payload), *payload])
        if not wait(host, lambda: flags(host, 1)):
            return f"packet {k}: no end of packet"
        f = flags(host, 1)
        got = [w >> 8 for w in host.pop(1, rx_level(host, 1))]
        if f != 1 or got[:2] != [0x80, pid] or got[2:-2] != payload:
            return f"packet {k}: IRQ {f:#04x}, bytes {bytes(got).hex()}, sent {bytes(payload).hex()}"
        clear_flags(host, 0)
        clear_flags(host, 1)
    return None


def frame_words(ident: int, data: list[int]) -> list[int]:
    bits = can_bits(ident, data)
    bits += [0] * (-len(bits) % 16)
    return [len(can_bits(ident, data))] + [sum(b << (15 - i) for i, b in enumerate(bits[k:k + 16]))
                                          for k in range(0, len(bits), 16)]


def check_can(host: Host) -> str | None:
    """can_tx and can_rx on one open-drain pad, pin 2, the pad's pull-up the recessive level: a
    wired-AND bus of two nodes. 125 kbit/s (CLKDIV 27). Each frame acknowledged by can_rx, its
    CRC right on both sides."""
    host.load(0, program("can/can_tx.asm"), clkdiv_int=27, out_base=2, set_base=2, in_base=2,
              open_drain_mask=1 << 2)
    host.load(1, program("can/can_rx.asm"), clkdiv_int=27, set_base=2, in_base=2,
              open_drain_mask=1 << 2)
    host.start(0b11)
    rnd = random.Random(8)
    frames = [(rnd.randrange(2048), [rnd.randrange(256) for _ in range(rnd.randrange(9))]) for _ in range(12)]
    frames[1] = (0x7EF, [0xFF, 0xFF])                     # long recessive runs (0x7F0 up: not legal)
    for k, (ident, data) in enumerate(frames):
        host.push(0, frame_words(ident, data))
        if not wait(host, lambda: flags(host, 0) and flags(host, 1), 20000):
            return f"frame {k}: IRQ {flags(host, 0):#x} / {flags(host, 1):#x}"
        tx, rx = flags(host, 0), flags(host, 1)
        words = host.pop(1, rx_level(host, 1))
        got = (words[0] >> 3 & 0x7FF, [w & 0xFF for w in words[2:]]) if len(words) >= 2 else None
        if tx != 1 or rx != 1 or got != (ident, data):
            return f"frame {k}: can_tx IRQ {tx:#x} (1: acknowledged), can_rx IRQ {rx:#x}, got {got}"
        clear_flags(host, 0)
        clear_flags(host, 1)
    return None


CODES = [0b11110, 0b01001, 0b10100, 0b10101, 0b01010, 0b01011, 0b01110, 0b01111,
         0b10010, 0b10011, 0b10110, 0b10111, 0b11010, 0b11011, 0b11100, 0b11101]


def check_fourb5b(host: Host) -> str | None:
    """fourb5b_tx on pin 3 into fourb5b_rx, BMC at CLKDIV 4 (844 kbit/s). The preamble is made of
    nibbles 4 and 3 (01010, 10101: alternating bits); nibble 2 (10100) starts with the two 0s
    that start the symbols."""
    host.load(0, program("fourb5b/fourb5b_tx.asm"), clkdiv_int=4, out_base=3, set_base=3)
    host.load(1, program("fourb5b/fourb5b_rx.asm"), clkdiv_int=4, in_base=3)
    rnd = random.Random(9)
    data = list(range(16)) + [rnd.randrange(16) for _ in range(24)]
    host.push(0, [2 * n for n in [4, 3] * 16 + [2] + data])
    host.start(0b11)
    wait_rx(host, 1, 1 + len(data))
    words = host.pop(1, rx_level(host, 1))
    if any(w & 0x7FF for w in words):
        return "bits outside a symbol"
    return differ("symbols", [CODES[2]] + [CODES[n] for n in data], [w >> 11 for w in words]) or (
        "PAU overrun" if flags(host, 1) & 1 else None)


def check_spi(host: Host) -> str | None:
    """spi_master (mode 0, SCK 1.125 MHz) and spi_pau_target: MOSI pin 0, SCK 1, CS_N 2, MISO 3,
    bytes both ways."""
    host.load(0, program("spi/spi_master.asm"), clkdiv_int=4, x=0)
    host.load(1, program("spi/spi_pau_target.asm"))
    rnd = random.Random(10)
    mosi, miso = [rnd.randrange(256) for _ in range(32)], [rnd.randrange(256) for _ in range(32)]
    host.push(1, [b << 8 for b in miso])
    host.start(0b11)
    host.push(0, [b << 8 for b in mosi])
    wait(host, lambda: rx_level(host, 0) >= 32 and rx_level(host, 1) >= 32)
    return (differ("MOSI", mosi, [w & 0xFF for w in host.pop(1, rx_level(host, 1))])
            or differ("MISO", miso, [w & 0xFF for w in host.pop(0, rx_level(host, 0))]))


def check_sd(host: Host) -> str | None:
    """sd_command (mode 0, the PAU's clock on SCK, 844 kHz) into spi_pau_target: MOSI pin 0,
    SCK 1, CS_N 2. Each command's six bytes, the CRC-7 appended by the PAU checked against CRC-7/SD."""
    host.load(0, program("sd/sd_command.asm"), clkdiv_int=4)
    host.load(1, program("spi/spi_pau_target.asm"))
    host.start(0b11)
    sent = []
    for index, arg in ((0, 0), (8, 0x1AA), (55, 0), (41, 0x40000000), (17, 0x00012345)):
        head = [0x40 | index, *(arg >> s & 0xFF for s in (24, 16, 8, 0))]
        sent += head + [crc7_sd(head) << 1 | 1]
        host.push(0, [b << 8 for b in head])
    wait_rx(host, 1, len(sent))
    got = [w & 0xFF for w in host.pop(1, rx_level(host, 1))]
    return differ("command bytes", sent, got) or (None if got[5] == 0x95 else "CMD0 does not end in 0x95")


def check_i2s(host: Host) -> str | None:
    """i2s_out (SDOUT pin 0, BCLK 1, LRCLK 2) into i2s_in reading SDOUT, in lockstep at
    CLKDIV 4 (BCLK 3.375 MHz): the samples come back in order, between silences."""
    host.load(0, program("i2s/i2s_out.asm"), clkdiv_int=4)
    host.load(1, program("i2s/i2s_in.asm"), clkdiv_int=4, in_base=0)
    rnd = random.Random(11)
    samples = [rnd.randrange(1, 1 << 16) for _ in range(32)]
    host.push(0, samples)
    host.start(0b11)
    wait_rx(host, 1, 100)
    got = host.pop(1, rx_level(host, 1))
    k = got.index(samples[0]) if samples[0] in got else -1
    return differ("samples", samples, got[k:k + len(samples)]) if k >= 0 else "the first sample never came"


def check_edges(host: Host) -> str | None:
    """edges.asm on SM 1 timestamps uart_tx's line on pin 0 (both CLKDIV 4, 844 kbaud): the
    recording, read as the learner reads one, gives the bytes back."""
    host.load(0, program("uart/uart_tx.asm"), clkdiv_int=4)
    host.load(1, program("logic/edges.asm"), clkdiv_int=4)
    host.start(0b11)
    data = [random.Random(12).randrange(256) for _ in range(20)]
    host.push(0, data)
    words, quiet = [], 0
    while quiet < 30:                                      # the RX FIFO drained as it fills
        n = rx_level(host, 1)
        words += host.pop(1, n) if n else []
        quiet = 0 if n else quiet + 1
    if len(words) < 2:
        return "no edge recorded"
    cap = Capture.from_edges_program(words)
    chars = characters(cap, AsyncSerial(8, 1, 8, None, 1))     # 8 strobes a bit
    return differ("bytes", data, [sum(b << k for k, b in enumerate(c[1:9])) for c in chars])


CHECKS = [check_uart, check_autobaud, check_duplex, check_hdlc, check_usb, check_can,
          check_fourb5b, check_spi, check_sd, check_i2s, check_edges]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("checks", nargs="*", help="names of checks to run (default: all)")
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--hz", type=int, default=1_000_000, help="SCLK (at most clk / 6)")
    ap.add_argument("--model", action="store_true", help="the Python model, not the Pico")
    args = ap.parse_args()
    names = {c.__name__[6:]: c for c in CHECKS}
    unknown = set(args.checks) - set(names)
    if unknown:
        ap.error(f"unknown checks {sorted(unknown)}: {', '.join(names)}")
    host = Host(Board(Model() if args.model else Serial(args.port), args.hz))
    failed = 0
    for name, check in names.items():
        if args.checks and name not in args.checks:
            continue
        release(host)
        error = check(host)
        print(f"{'FAIL' if error else 'ok':4}  {name:9} {error or ''}".rstrip(), flush=True)
        failed += error is not None
    release(host)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
