"""board.py - The chip on a board, driven from the PC through the Pico bridge (pico/main.py).

Board has the SPI method of the model's Chip, so the reference driver runs on it unchanged:

    board = Board(Serial("/dev/ttyACM0"))
    host = Host(board)
    host.load(0, assemble(source))

Serial is the Pico's USB port (Linux and macOS, standard library only). Model is the same
bridge over the Python model of the chip, with the board's pads and maybe recordings played
on its pins, for running the bring-up checks and listen.py without hardware.
"""

from __future__ import annotations

import os
import select
import sys
import termios
import tty
from pathlib import Path
from typing import Callable, Sequence

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "sim" / "model"))
sys.path.insert(0, str(HERE.parent / "sim" / "learn"))
sys.path.insert(0, str(HERE / "pico"))
from chip import TT_PADS, Chip                           # noqa: E402
from learn import Capture                                # noqa: E402
import main as bridge                                    # noqa: E402


class Serial:
    """A USB serial port in raw mode: one line out, one line back."""

    def __init__(self, port: str, timeout: float = 2.0) -> None:
        self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY)
        tty.setraw(self.fd)
        termios.tcflush(self.fd, termios.TCIOFLUSH)
        self.timeout, self.buf = timeout, b""

    def ask(self, line: str) -> str:
        os.write(self.fd, line.encode() + b"\n")
        while b"\n" not in self.buf:
            if not select.select([self.fd], [], [], self.timeout)[0]:
                raise TimeoutError(f"no answer to {line[:40]!r}: is pico/main.py on the Pico?")
            self.buf += os.read(self.fd, 4096)
        answer, _, self.buf = self.buf.partition(b"\n")
        return answer.decode(errors="replace").strip()

    def close(self) -> None:
        os.close(self.fd)


class Pads(Chip):
    """The model chip on the board: a bidirectional pin it drives reads its own value back, an
    undriven one the recording played on it, else its pull-up (tang_top.v, the cst file). The
    recordings start at clock `t0`, once it is set; until then they hold their first level."""

    def __init__(self, play: dict[int, Capture] | None = None) -> None:
        super().__init__()
        self.play = play or {}
        self.played = sum(1 << k for k in self.play)
        self.t0: int | None = None

    def step(self, cycles: int = 1) -> None:
        for _ in range(cycles):
            value, oe = self.pins
            t = -1 if self.t0 is None else self.cycle - self.t0
            ext = sum(c.level_at(t) << k for k, c in self.play.items())
            self.pin_in = value & oe | ~oe & (TT_PADS[2] & ~self.played | ext)
            super().step()


class Model:
    """The bridge's firmware over the model chip, in place of Serial; `play`: recordings
    (times in clocks) on pins, from `chip.t0`."""

    def __init__(self, play: dict[int, Capture] | None = None) -> None:
        self.chip = Pads(play)

    def xfer(self, out: bytes) -> bytes:
        words = [out[i] << 8 | out[i + 1] for i in range(0, len(out), 2)]
        miso = self.chip.spi(words[0], words[1:])
        return bytes(b for w in miso for b in (w >> 8, w & 0xFF))

    def ask(self, line: str) -> str:
        return bridge.reply(line, self.xfer, lambda: self.chip.host_irq, lambda hz: None)


class Board:
    """The chip behind the bridge, with Chip's SPI method for Host."""

    def __init__(self, port: Serial | Model, hz: int = 1_000_000) -> None:
        self.port = port
        self.ask("?", lambda a: a == "openpio-bridge 1", "not the openPIO bridge")
        self.ask(f"f {hz}")

    def ask(self, line: str, ok: Callable[[str], bool] = lambda a: True,
            what: str = "") -> str:
        answer = self.port.ask(line)
        if answer.startswith("e") or not ok(answer):
            raise IOError(f"bridge: {line[:40]!r} -> {answer[:60]!r} {what}".rstrip())
        return answer

    def spi(self, hdr: int, words: Sequence[int] = ()) -> list[int]:
        """One SPI transaction: the MISO words, the header's first (Chip.spi)."""
        line = " ".join(f"{w & 0xFFFF:04x}" for w in (hdr, *words))
        miso = [int(w, 16) for w in self.ask(line).split()]
        if len(miso) != 1 + len(words):
            raise IOError(f"bridge: {len(miso)} MISO words for {1 + len(words)}")
        return miso

    @property
    def host_irq(self) -> int:
        return int(self.ask("i", lambda a: a in ("0", "1")))
