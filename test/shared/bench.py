"""bench.py - pin-level test bench around the chip model (sim/model/chip.py).

The chip drives its pins (value and output enable, after open drain); devices on the board drive
or pull them; each pin resolves to one level, which is what the chip and the devices see. A pin
without a bidirectional pad has two wires: the devices see the chip's output pad and drive its
input pad. Tests judge a program only by the levels on the pins and by the data it exchanges with
the host.

    bench = Bench(pullups=0b11)
    bench.load(0, source, set_base=0)      # the program's configuration, set_base overridden
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
from chip import OP_RX_POP, OP_TX_PUSH, Chip     # noqa: E402
from host import Host             # noqa: E402


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


class Script(Device):
    """Drives pins from a list of (clock, {pin: level or None}); None releases the pin."""

    def __init__(self, steps):
        super().__init__()
        self.steps = sorted(steps, key=lambda s: s[0])
        self.tick(-1, 0)                                  # levels of clock 0

    def tick(self, t, levels):
        while self.steps and self.steps[0][0] <= t + 1:
            for pin, lv in self.steps.pop(0)[1].items():
                if lv is None:
                    self.drive.pop(pin, None)
                else:
                    self.drive[pin] = lv


class Bench:
    """Chip model + devices + host, clocked together."""

    chip_class = Chip               # test/lockstep/ puts the RTL beside the model

    def __init__(self, n_sm: int = 2, pullups: int = 0, host_word_clocks: int = 0) -> None:
        """host_word_clocks: clocks the host needs per FIFO word (an SPI host: about 100);
        0 = one word per clock."""
        self.chip = self.chip_class(n_sm)
        self.host = Host(self.chip)
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
        """Assemble (if text) and load a program into SM `sm` through the host's register
        writes; cfg overrides the program's configuration (host.Host.load)."""
        if isinstance(program, str):
            program = assemble(program)
        self.host.load(sm, program, **cfg)
        return program

    def add(self, device: Device) -> Device:
        self.devices.append(device)
        return device

    def start(self, mask: int = 1) -> None:
        self.host.start(mask)

    def send(self, sm: int, words: Sequence[int]) -> None:
        """Queue words for the host to push into the TX FIFO as soon as there is room."""
        self.to_send[sm].extend(w & 0xFFFF for w in words)

    # ------------------------------------------------------------------ running

    def _resolve(self, low: int, high: int) -> int:
        """Levels of the 16 pins driven low / high by these masks: any 0 wins (and both is
        contention), an undriven pin has its pull-up."""
        for pin in range(16) if low & high else ():
            if low & high >> pin & 1:
                self.contention.append(f"t={self.t}: pin {pin} driven 0 and 1")
        return ~low & (high | self.pullups) & 0xFFFF

    def levels(self) -> tuple[int, int]:
        """(levels the devices see, levels at the chip's input pads) of the 16 pins, from the
        chip outputs and the device drives."""
        out, oe = self.chip.pins
        dev_low = dev_high = 0
        for d in self.devices:
            for pin, lv in d.drive.items():
                if lv:
                    dev_high |= 1 << pin
                else:
                    dev_low |= 1 << pin
        chip_low, chip_high = oe & ~out, oe & out
        bidir, split = self.chip.pads_bidir, ~self.chip.pads_bidir
        one = self._resolve((dev_low | chip_low) & bidir, (dev_high | chip_high) & bidir) & bidir
        seen = self._resolve(chip_low & split, chip_high & split) & split        # two wires
        chip_in = self._resolve(dev_low & split, dev_high & split) & split
        return one | seen, one | chip_in

    def run(self, cycles: int, until: Callable[[], bool] | None = None) -> int:
        """Run up to `cycles` clocks, stopping after the clock where until() is true. The host
        makes at most one access a clock (chip.host()): an RX_POP of a word the OUT register
        holds, else a TX_PUSH where the IN register is free, the SMs taken in turn."""
        for n in range(cycles):
            host_turn = not self.host_word_clocks or self.t % self.host_word_clocks == 0
            access = self._host_access() if host_turn else None
            lv, self.chip.pin_in = self.levels()
            self.trace.append(lv)
            for d in self.devices:
                d.tick(self.t, lv)
            if access is None:
                self.chip.step()
            elif access[0] == OP_RX_POP:
                self.received[access[1]].append(self.chip.host(*access))
            else:
                self.chip.host(*access)
                self.to_send[access[1]].pop(0)
            self.t += 1
            if until is not None and until():
                return n + 1
        return cycles

    def _host_access(self) -> tuple[int, int, int, int] | None:
        sms = self.chip.sms
        for k in range(len(sms)):
            sm = (self.t + k) % len(sms)
            if sms[sm].rx_out is not None:
                return OP_RX_POP, sm, 0, 0
        for k in range(len(sms)):
            sm = (self.t + k) % len(sms)
            if self.to_send[sm] and sms[sm].tx_room():
                return OP_TX_PUSH, sm, 0, self.to_send[sm][0]
        return None

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
