#!/usr/bin/env python3
"""peripherals.py - The protocol programs against the Pico's own UART, SPI and I2C: hardware
written by Raspberry Pi, driven by MicroPython, neither of them written by this project, so a
misreading of a protocol shared by the programs and the reference devices in test/shared would
show here. The bridge's firmware (pico/main.py) drives the Pico's peripherals; this drives the
chip and compares.

Wiring (board/README.md), besides the bridge's: pio 0-3 SPI (SCK GP10, MOSI GP11, CS_N GP13,
MISO GP12), pio 4 the chip's UART TX into GP9, pio 5 its UART RX from GP8, pio 6 SCL GP15,
pio 7 SDA GP14, with a pull-up of about 5 kohm from each of SCL and SDA to 3V3.

    python3 board/peripherals.py              # the Pico on /dev/ttyACM0
    python3 board/peripherals.py uart_out     # some checks only
    python3 board/peripherals.py --sigrok     # and a logic analyzer's judgement

With --sigrok, a logic analyzer with D0-D7 on pio 0-7 records the protocol checks (sigrok-cli,
24 MHz), and sigrok's decoders must read on the wires what each check sent and received.

The rates are those of the board's 27 MHz clock.
"""

from __future__ import annotations

import argparse
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from board import Board, Serial
from loopback import clear_flags, differ, flags, program, release, rx_level, wait, wait_rx
from asm import assemble                                 # noqa: E402  (board put it on the path)
from chip import CTRL, FIFO_LEVEL, PINS_IN               # noqa: E402
from host import Host                                    # noqa: E402

CLK = 27e6
BAUDS = (9600, 115200, 1_000_000, 3_000_000)
SPI_HZ = (250_000, 500_000, 900_000)                     # spi_target: SCK up to clk / 28
I2C_HZ = (100_000, 400_000)
TARGET = 0x42                                            # the Pico's I2C address
SEEN: list[tuple] = []          # what the checks put on the wires, for judge()


class Pico:
    """The Pico's peripherals through the bridge's firmware."""

    def __init__(self, board: Board) -> None:
        self.board = board

    def ask(self, *words) -> str:
        line = " ".join(str(w) for w in words)
        answer = self.board.port.ask(line)
        if not answer.startswith("r"):
            raise IOError(f"Pico: {line[:40]!r} -> {answer[:80]!r}")
        return answer[1:].strip()

    def bytes(self, *words) -> list[int]:
        return list(bytes.fromhex(self.ask(*words)))


def divider(rate: float, steps: int) -> tuple[int, int]:
    """CLKDIV for `steps` strobes a bit at `rate`, integer and 1/256ths."""
    d = round(CLK / steps / rate * 256)
    return d >> 8, d & 0xFF


# The wires: (the Pico's GPIO, the chip's pio, who drives it: the Pico, the chip, or both)
WIRES = [(10, 0, "pico"), (11, 1, "pico"), (13, 2, "pico"), (12, 3, "chip"), (9, 4, "chip"),
         (8, 5, "pico"), (15, 6, "both"), (14, 7, "both")]

DRIVE = """
.program drive
.pin_mask 0, 1, 2, 3, 4, 5, 6, 7
.wrap_target
    pull block              ; the directions, then the levels
    out pindirs, 8
    pull block
    out pins, 8
.wrap
"""


def tx_level(host: Host, n: int) -> int:
    return host.read(n, FIFO_LEVEL)[0] & 0xFF


def check_wires(host: Host, pico: Pico) -> str | None:
    """Each wire alone, both levels, in its direction: the Pico drives its GPIO and the chip
    reads PINS_IN, or the chip drives its pio pin and the Pico reads its GPIO; the other wires
    held at the opposite level, so that two swapped or touching wires show too."""
    host.write(0, CTRL, 0x30)
    host.load(0, assemble(DRIVE))
    host.start(0b01)
    bad = []
    for gp, pio, who in WIRES:
        for level in (0, 1):
            k = gp - 8
            if who in ("pico", "both"):
                host.push(0, [0, 0])                     # the chip drives nothing
                rest = sum(1 << (g - 8) for g, _, w in WIRES if w in ("pico", "both"))
                pico.ask("g", f"{rest:02x}", f"{(1 << k if level else 0xFF ^ 1 << k) & 0xFF:02x}")
                time.sleep(0.002)
                got = host.read(0, PINS_IN)[0] >> pio & 1
                if got != level:
                    bad.append(f"GP{gp} -> pio {pio} at {level}: {got}")
            if who in ("chip", "both"):
                pico.ask("g", "00", "00")                # the Pico drives nothing
                mine = sum(1 << p for _, p, w in WIRES if w == "chip") | 1 << pio
                host.push(0, [mine, 1 << pio if level else 0xFF ^ 1 << pio])
                time.sleep(0.002)
                got = int(pico.ask("g", "00", "00"), 16) >> k & 1
                if got != level:
                    bad.append(f"pio {pio} -> GP{gp} at {level}: {got}")
    host.push(0, [0, 0])
    pico.ask("x")
    return "; ".join(bad) or None


def check_uart_out(host: Host, pico: Pico) -> str | None:
    """uart_tx on pio 4 into the Pico's UART1 RX, 128 bytes at each rate."""
    for baud in BAUDS:
        div, frac = divider(baud, 8)
        pico.ask("u", baud)
        host.write(0, CTRL, 0x30)
        host.load(0, program("uart/uart_tx.asm"), clkdiv_int=div, clkdiv_frac=frac,
                  out_base=4, set_base=4)
        data = random.Random(baud).choices(range(256), k=128)
        host.push(0, data)
        host.start(0b01)
        wait(host, lambda: tx_level(host, 0) == 0, 20000)
        time.sleep(30 / baud + 0.01)                     # the last byte, and the Pico's IRQ
        SEEN.append(("uart", 4, baud, data))
        error = differ(f"{baud} baud", data, pico.bytes("ur"))
        if error:
            return error
    return None


def check_uart_in(host: Host, pico: Pico) -> str | None:
    """The Pico's UART1 TX into uart_rx on pio 5, 128 bytes at each rate, no framing error."""
    for baud in BAUDS:
        div, frac = divider(baud, 8)
        pico.ask("u", baud)
        host.write(0, CTRL, 0x30)
        host.load(1, program("uart/uart_rx.asm"), clkdiv_int=div, clkdiv_frac=frac, in_base=5)
        clear_flags(host, 1)
        host.start(0b10)
        data = random.Random(-baud).choices(range(256), k=128)
        pico.ask("ut", bytes(data).hex())
        SEEN.append(("uart", 5, baud, data))
        wait_rx(host, 1, len(data), 20000)
        error = differ(f"{baud} baud", data, [w >> 8 for w in host.pop(1, rx_level(host, 1))])
        if error or flags(host, 1) & 1:
            return error or f"{baud} baud: framing error"
    return None


def check_spi(host: Host, pico: Pico) -> str | None:
    """The Pico's SPI1 controller, mode 0, into spi_target on pio 0-3: transfers of 1, 7, 32
    and 24 bytes, the answers from the TX FIFO in order, at each SCK."""
    for hz in SPI_HZ:
        rnd = random.Random(hz)
        transfers = [[rnd.randrange(256) for _ in range(n)] for n in (1, 7, 32, 24)]
        answers = [rnd.randrange(256) for _ in range(64)]
        host.write(0, CTRL, 0x30)
        host.load(0, program("spi/spi_target.asm"))
        host.push(0, [a << 8 for a in answers])
        host.start(0b01)
        real = pico.ask("s", hz)
        miso = [b for t in transfers for b in pico.bytes("sx", bytes(t).hex())]
        mosi = [w & 0xFF for w in host.pop(0, rx_level(host, 0))]
        SEEN.append(("spi", hz, sum(transfers, []), answers))
        error = (differ(f"SCK {real} Hz, MOSI", sum(transfers, []), mosi)
                 or differ(f"SCK {real} Hz, MISO", answers, miso))
        if error:
            return error
    return None


def cmd(data: int = 0, start: int = 0, read: int = 0, nack: int = 0, stop: int = 0) -> int:
    """An i2c_master command word (programs/i2c/i2c_master.asm)."""
    return start << 15 | read << 14 | (data & 0xFF) << 6 | nack << 5 | stop << 4


def i2c(host: Host, cmds: list[int]) -> list[int] | None:
    """The commands through i2c_master: a result word per byte, bits 8:1 the byte seen on SDA,
    bit 0 its ACK (0: ACK)."""
    host.push(0, cmds)
    if not wait_rx(host, 0, len(cmds), 20000):
        return None
    return host.pop(0, rx_level(host, 0))


def check_i2c(host: Host, pico: Pico) -> str | None:
    """i2c_master on pio 6 (SCL) and 7 (SDA), open drain, against the Pico's I2C1 target, a
    memory of 256 bytes: 16 bytes written from a register and found in the Pico's memory, 16
    read back from another (repeated START) as the Pico holds them, and an address nobody
    answers NACKed, at each SCL."""
    for hz in I2C_HZ:
        rnd = random.Random(hz)
        div, frac = divider(hz, 11)
        pico.ask("t", f"{TARGET:x}")
        memory = [rnd.randrange(256) for _ in range(256)]
        pico.ask("tw", "0", bytes(memory).hex())
        host.write(0, CTRL, 0x30)
        host.load(0, program("i2c/i2c_master.asm"), clkdiv_int=div, clkdiv_frac=frac,
                  set_base=7, out_base=7, in_base=7, sideset_base=6,
                  pin_mask=0xC0, open_drain_mask=0xC0)
        host.start(0b01)

        reg, data = rnd.randrange(256 - 16), [rnd.randrange(256) for _ in range(16)]
        got = i2c(host, [cmd(TARGET << 1, start=1), cmd(reg)] + [cmd(b) for b in data[:-1]]
                  + [cmd(data[-1], stop=1)])
        if got is None:
            return f"{hz} Hz write: {rx_level(host, 0)} results"
        seen = [TARGET << 1, reg] + data
        SEEN.append(("i2c", hz, ["Start", f"Address write: {TARGET:02X}", "ACK"]
                     + [x for b in [reg] + data for x in (f"Data write: {b:02X}", "ACK")]
                     + ["Stop"]))
        if [w >> 1 & 0xFF for w in got] != seen or any(w & 1 for w in got):
            return f"{hz} Hz write: results {[f'{w:03x}' for w in got]}"
        error = differ(f"{hz} Hz, the Pico's memory", data, pico.bytes("tm", f"{reg:x}", 16))
        if error:
            return error

        reg = rnd.randrange(256 - 16)
        expect = pico.bytes("tm", f"{reg:x}", 16)
        got = i2c(host, [cmd(TARGET << 1, start=1), cmd(reg), cmd(TARGET << 1 | 1, start=1)]
                  + [cmd(read=1)] * 15 + [cmd(read=1, nack=1, stop=1)])
        if got is None:
            return f"{hz} Hz read: {rx_level(host, 0)} results"
        if any(w & 1 for w in got[:3]):
            return f"{hz} Hz read: address or register not ACKed: {[f'{w:03x}' for w in got[:3]]}"
        SEEN.append(("i2c", hz, ["Start", f"Address write: {TARGET:02X}", "ACK",
                                 f"Data write: {reg:02X}", "ACK", "Start repeat",
                                 f"Address read: {TARGET:02X}", "ACK"]
                     + [x for b in expect[:-1] for x in (f"Data read: {b:02X}", "ACK")]
                     + [f"Data read: {expect[-1]:02X}", "NACK", "Stop"]))
        error = differ(f"{hz} Hz read", expect, [w >> 1 & 0xFF for w in got[3:]])
        if error:
            return error

        got = i2c(host, [cmd((TARGET + 1) << 1, start=1, stop=1)])
        SEEN.append(("i2c", hz, ["Start", f"Address write: {TARGET + 1:02X}", "NACK", "Stop"]))
        if got is None or got[0] & 1 == 0:
            return f"{hz} Hz: address {TARGET + 1:#x} ACKed by nobody? {got}"
    return None


def sigrok(path: Path, decoder: str, annotations: str) -> list[str]:
    """The annotations of one of sigrok's decoders on the recording, in order."""
    out = subprocess.run(["sigrok-cli", "-i", str(path), "-P", decoder, "-A", annotations],
                         capture_output=True, text=True, check=True,
                         env=dict(os.environ, PYTHONWARNINGS="ignore::SyntaxWarning")).stdout
    return [line.split(": ", 1)[1] for line in out.splitlines() if ": " in line]


def within(seq: list, part: list) -> bool:
    return any(seq[i:i + len(part)] == part for i in range(len(seq) - len(part) + 1))


def judge(path: Path) -> list[str]:
    """What sigrok's decoders do not read on the wires of what the checks sent (SEEN)."""
    bad = []
    spi = "spi:clk=D0:mosi=D1:cs=D2:miso=D3:cs_polarity=active-low"
    for kind, *what in SEEN:
        if kind == "uart":
            pin, baud, data = what
            got = [int(b, 16) for b in sigrok(path, f"uart:rx=D{pin}:baudrate={baud}",
                                               "uart=rx-data")]
            if not within(got, data):
                bad.append(f"UART on pio {pin} at {baud} baud")
        elif kind == "spi":
            hz, mosi, miso = what
            if not (within([int(b, 16) for b in sigrok(path, spi, "spi=mosi-data")], mosi)
                    and within([int(b, 16) for b in sigrok(path, spi, "spi=miso-data")], miso)):
                bad.append(f"SPI at {hz} Hz")
        else:
            hz, part = what
            got = [a for a in sigrok(path, "i2c:scl=D6:sda=D7", "i2c")
                   if a not in ("0", "1", "Write", "Read")]
            if not within(got, part):
                bad.append(f"I2C at {hz} Hz: {' '.join(part[:2])}...")
    return bad


CHECKS = [check_wires, check_uart_out, check_uart_in, check_spi, check_i2c]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("checks", nargs="*", help="names of checks to run (default: all)")
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--hz", type=int, default=1_000_000, help="the bridge's SCLK")
    ap.add_argument("--sigrok", action="store_true",
                    help="record the protocol checks with a logic analyzer (D0-D7 on pio 0-7) "
                         "and judge the wires with sigrok's decoders")
    ap.add_argument("--driver", default="fx2lafw", help="the analyzer's sigrok driver")
    args = ap.parse_args()
    names = {c.__name__[6:]: c for c in CHECKS}
    unknown = set(args.checks) - set(names)
    if unknown:
        ap.error(f"unknown checks {sorted(unknown)}: {', '.join(names)}")
    board = Board(Serial(args.port), args.hz)
    host, pico = Host(board), Pico(board)
    print(f"the Pico: MicroPython {pico.ask('pv')}")
    chosen = [(n, c) for n, c in names.items() if not args.checks or n in args.checks]
    failed = 0

    def run(name, check) -> None:
        nonlocal failed
        release(host)
        try:
            error = check(host, pico)
        except IOError as e:
            error = str(e)
        print(f"{'FAIL' if error else 'ok':4}  {name:9} {error or ''}".rstrip(), flush=True)
        failed += error is not None

    for name, check in chosen:              # the wires first: they move every line at random,
        if check is check_wires:            # which no decoder should have to read
            run(name, check)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "peripherals.sr"
        if args.sigrok:
            rec = subprocess.Popen(["sigrok-cli", "-d", args.driver, "--config",
                                    "samplerate=24m", "--time", "5s", "-o", str(path)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1.5)                 # the analyzer starts
        for name, check in chosen:
            if check is not check_wires:
                run(name, check)
        release(host)
        pico.ask("x")
        if args.sigrok:
            rec.wait()
            bad = judge(path) if path.exists() else ["no recording: is the analyzer there?"]
            print(f"{'FAIL' if bad else 'ok':4}  sigrok    "
                  f"{'; '.join(bad) or f'the decoders read all {len(SEEN)} on the wires'}")
            failed += bool(bad)
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main())
