"""bench.py - pin-level test bench around the chip model (sim/model/chip.py).

The chip drives its pins (value and output enable, after open drain); devices on the board drive
or pull them; each pin resolves to one level, which is what the chip and the devices see. Tests
judge a program only by the levels on the pins and by the data it exchanges with the host.

    bench = Bench(pullups=0b11)
    bench.load(0, source, set_base=0)
    dev = bench.add(SomeDevice(...))
    bench.start(0b01)
    bench.run(1000, until=lambda: ...)

Each clock: the levels are resolved from the chip outputs and the device drives set at the
previous clock, the devices see them and set their drives for the next clock, the chip steps with
them as inputs. A device therefore answers one clock after what it sees, like a real one.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sim" / "model"))
from asm import assemble          # noqa: E402
from chip import CTRL, Chip       # noqa: E402


class Device:
    """Something on the board. `drive` maps pin -> level it drives (open-drain devices only
    ever drive 0); `errors` collects protocol violations it observed."""

    def __init__(self) -> None:
        self.drive: dict[int, int] = {}
        self.errors: list[str] = []

    def tick(self, t: int, levels: int) -> None:
        """See the pin levels of clock t; update self.drive for clock t + 1."""

    def error(self, t: int, msg: str) -> None:
        self.errors.append(f"t={t}: {msg}")


class Bench:
    """Chip model + devices + host, clocked together."""

    def __init__(self, n_sm: int = 2, pullups: int = 0, host_word_clocks: int = 0) -> None:
        """host_word_clocks: clocks the host needs per FIFO word (an SPI host: about 100);
        0 = one word per clock."""
        self.chip = Chip(n_sm)
        self.host_word_clocks = host_word_clocks
        self.pullups = pullups
        self.devices: list[Device] = []
        self.trace: list[int] = []                      # resolved levels, one per clock
        self.contention: list[str] = []
        self.to_send: list[list[int]] = [[] for _ in self.chip.sms]
        self.received: list[list[int]] = [[] for _ in self.chip.sms]
        self.t = 0

    # ------------------------------------------------------------------ setup

    def load(self, sm: int, program, **cfg):
        """Assemble (if text) and load a program into SM `sm`; cfg as chip.Chip.load."""
        if isinstance(program, str):
            program = assemble(program)
        self.chip.load(sm, program, **cfg)
        return program

    def add(self, device: Device) -> Device:
        self.devices.append(device)
        return device

    def start(self, mask: int = 1) -> None:
        self.chip.reg_write(0, CTRL, mask)

    def send(self, sm: int, words: Sequence[int]) -> None:
        """Queue words for the host to push into the TX FIFO as soon as there is room."""
        self.to_send[sm].extend(w & 0xFFFF for w in words)

    # ------------------------------------------------------------------ running

    def levels(self) -> int:
        """Resolve the 8 pins from the chip outputs and the device drives."""
        out, oe = self.chip.pins
        level = 0
        for pin in range(8):
            dev = [d.drive[pin] for d in self.devices if pin in d.drive]
            chip = (out >> pin & 1) if oe >> pin & 1 else None
            drivers = dev + ([chip] if chip is not None else [])
            if 0 in drivers and 1 in drivers:
                self.contention.append(f"t={self.t}: pin {pin} driven 0 and 1")
            if drivers:
                bit = 0 if 0 in drivers else 1
            else:
                bit = self.pullups >> pin & 1
            level |= bit << pin
        return level

    def run(self, cycles: int, until: Callable[[], bool] | None = None) -> int:
        """Run up to `cycles` clocks, stopping after the clock where until() is true."""
        for n in range(cycles):
            host_turn = not self.host_word_clocks or self.t % self.host_word_clocks == 0
            for sm, words in enumerate(self.to_send):
                if host_turn and words and self.chip.tx_push(sm, words[0]):
                    words.pop(0)
            lv = self.levels()
            self.trace.append(lv)
            for d in self.devices:
                d.tick(self.t, lv)
            self.chip.uio_in = lv
            self.chip.step()
            for sm, s in enumerate(self.chip.sms):
                while host_turn and s.rx_level:
                    self.received[sm].append(self.chip.rx_pop(sm))
                    if self.host_word_clocks:
                        break
            self.t += 1
            if until is not None and until():
                return n + 1
        return cycles

    # ------------------------------------------------------------------ measurements

    def wave(self, pin: int, start: int = 0, end: int | None = None) -> str:
        """The levels of `pin` as a string of 0 / 1, one character per clock."""
        return "".join(str(lv >> pin & 1) for lv in self.trace[start:end])

    def runs(self, pin: int, start: int = 0, end: int | None = None) -> list[tuple[int, int]]:
        """[(level, clocks)] of consecutive equal levels of `pin`."""
        out: list[tuple[int, int]] = []
        for ch in self.wave(pin, start, end):
            b = int(ch)
            if out and out[-1][0] == b:
                out[-1] = (b, out[-1][1] + 1)
            else:
                out.append((b, 1))
        return out

    def edges(self, pin: int) -> list[tuple[int, int]]:
        """[(clock, new level)] of every change of `pin`."""
        w = self.wave(pin)
        return [(t, int(w[t])) for t in range(1, len(w)) if w[t] != w[t - 1]]

    def errors(self) -> list[str]:
        """Contention and every device's protocol errors."""
        return self.contention + [f"{type(d).__name__}: {e}" for d in self.devices for e in d.errors]
