#!/usr/bin/env python3
"""bringup.py - First checks of a chip (or the FPGA build) through the Pico bridge, wire by
wire: the SPI lines, registers, program memory, FIFOs, HOST_IRQ, then a UART on pin 0 read
back by the other SM. No wires besides the bridge's.

    python3 board/bringup.py                  # the Pico on /dev/ttyACM0
    python3 board/bringup.py /dev/ttyACM1 --hz 2000000
    python3 board/bringup.py --model          # the same checks on the Python model
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

from board import Board, Model, Serial

ROOT = Path(__file__).resolve().parents[1]
from asm import assemble                                 # noqa: E402  (board put it on the path)
from chip import (CRC_POLY_LO, CTRL, FIFO_LEVEL, ID, IRQ, ISA_VERSION, OP_IMEM_RD,   # noqa: E402
                  OP_IMEM_WR, PADS_BIDIR, TT_PADS, header)
from host import Host                                    # noqa: E402

ECHO = """
.program echo
    pull block
    mov isr, osr
    push block
"""


def wait_rx(host: Host, n: int, count: int, polls: int = 3000) -> bool:
    """Poll FIFO_LEVEL until SM n's RX FIFO holds `count` words."""
    return any(host.read(n, FIFO_LEVEL)[0] >> 8 >= count for _ in range(polls))


def check_id(board: Board, host: Host) -> str | None:
    ident, bidir = host.read(0, ID)[0], host.read(0, PADS_BIDIR)[0]
    if ident != (ISA_VERSION << 12 | 2 << 8 | 0x50):
        return f"ID {ident:#06x}, not {ISA_VERSION << 12 | 0x250:#06x}: MISO stuck, SCLK too fast?"
    if bidir != TT_PADS[2]:
        return f"PADS_BIDIR {bidir:#06x}"
    return None


def check_lines(board: Board, host: Host) -> str | None:
    """Each MOSI and MISO bit both ways, in a register of each SM."""
    for n in (0, 1):
        for v in [1 << i for i in range(16)] + [0xFFFF ^ 1 << i for i in range(16)]:
            host.write(n, CRC_POLY_LO, v)
            got = host.read(n, CRC_POLY_LO)[0]
            if got != v:
                return f"SM {n}: wrote {v:#06x}, read {got:#06x} (bits {v ^ got:#06x})"
    return None


def check_imem(board: Board, host: Host) -> str | None:
    rng = random.Random(1)
    for n in (0, 1):
        words = [rng.randrange(1 << 16) for _ in range(64)]
        board.spi(header(OP_IMEM_WR, n), [0, *words])
        got = board.spi(header(OP_IMEM_RD, n), [0] * 65)[2:]
        bad = [a for a in range(64) if got[a] != words[a]]
        if bad:
            a = bad[0]
            return f"SM {n}: {len(bad)} words differ, at {a}: {words[a]:#06x} -> {got[a]:#06x}"
    return None


def check_irq(board: Board, host: Host) -> str | None:
    if board.host_irq:
        return "HOST_IRQ high after reset"
    host.write(1, IRQ, 0x0800)                             # set flag 3 of SM 1
    if not board.host_irq:
        return "HOST_IRQ low with a flag set (wire to the Pico?)"
    host.write(1, IRQ, 0x0008)
    if board.host_irq:
        return "HOST_IRQ stays high after the flag is cleared"
    return None


def check_echo(board: Board, host: Host) -> str | None:
    """100 words through both FIFOs of SM 0 and its program."""
    host.load(0, assemble(ECHO))
    host.start(0b01)
    rng = random.Random(2)
    words = [rng.randrange(1 << 16) for _ in range(100)]
    host.push(0, words)
    if not wait_rx(host, 0, len(words)):
        return f"RX FIFO holds {host.read(0, FIFO_LEVEL)[0] >> 8} words of {len(words)}"
    got = host.pop(0, len(words))
    bad = [i for i in range(len(words)) if got[i] != words[i]]
    if bad:
        i = bad[0]
        return f"{len(bad)} words differ, word {i}: sent {words[i]:#06x}, got {got[i]:#06x}"
    return None


def check_uart(board: Board, host: Host) -> str | None:
    """SM 0 sends bytes on pin 0, SM 1 reads them back from the same pad."""
    uart = ROOT / "programs" / "uart"
    host.load(0, assemble((uart / "uart_tx.asm").read_text()), clkdiv_int=4)
    host.load(1, assemble((uart / "uart_rx.asm").read_text()), clkdiv_int=4, in_base=0)
    host.start(0b11)
    rng = random.Random(3)
    data = [rng.randrange(256) for _ in range(32)]
    host.push(0, data)
    if not wait_rx(host, 1, len(data)):
        return f"{host.read(1, FIFO_LEVEL)[0] >> 8} bytes of {len(data)} back: pin 0 loaded?"
    got = [w >> 8 for w in host.pop(1, len(data))]
    if got != data:
        return f"bytes differ: sent {bytes(data[:8]).hex()}.., got {bytes(got[:8]).hex()}.."
    if host.read(1, IRQ)[0] & 1:
        return "framing error flagged"
    return None


CHECKS = [check_id, check_lines, check_imem, check_irq, check_echo, check_uart]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("port", nargs="?", default="/dev/ttyACM0")
    ap.add_argument("--hz", type=int, default=1_000_000, help="SCLK (at most clk / 6)")
    ap.add_argument("--model", action="store_true", help="the Python model, not the Pico")
    args = ap.parse_args()
    board = Board(Model() if args.model else Serial(args.port), args.hz)
    host = Host(board)
    failed = 0
    for check in CHECKS:
        host.write(0, CTRL, 0x30)                          # stop and reset both SMs
        error = check(board, host)
        name = check.__name__[6:]
        print(f"{'FAIL' if error else 'ok':4}  {name:6} {error or ''}".rstrip())
        failed += error is not None
        if error and check is check_id:
            break                                          # nothing else can work
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
