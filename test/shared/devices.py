"""devices.py - reference devices for the test bench (bench.py), written from the protocol
definitions and independent of the emulator: they judge the waveforms on the pins.

Times are in chip clocks. Every device checks what a real one relies on (bit timing, setup and
hold around clock edges, line idle levels, framing) and lists violations in `errors`.
"""
from __future__ import annotations

from bench import Device


def _bit(levels: int, pin: int) -> int:
    return levels >> pin & 1


# ---------------------------------------------------------------------------------- UART

class UartRx(Device):
    """UART receiver: idle 1, start 0, data LSB first, optional parity, stop bits 1.

    A frame starts at a falling edge on an idle line; bit k is sampled in the middle of its
    nominal time. `frames` gets the data values; `skew` is the largest distance, in clocks, of an
    edge inside a frame from a nominal bit boundary (0 for an exact transmitter)."""

    def __init__(self, pin: int, bit: int, data_bits: int = 8, parity: str | None = None,
                 stop_bits: int = 1) -> None:
        super().__init__()
        self.pin, self.bit, self.data_bits = pin, bit, data_bits
        self.parity, self.stop_bits = parity, stop_bits
        self.nbits = 1 + data_bits + (parity is not None) + stop_bits
        self.frames: list[int] = []
        self.skew = 0
        self._prev = 1
        self._t0: int | None = None
        self._samples: list[int] = []

    def tick(self, t: int, levels: int) -> None:
        lv = _bit(levels, self.pin)
        if self._t0 is None:
            if self._prev == 1 and lv == 0:
                self._t0, self._samples = t, []
        else:
            rel = t - self._t0
            if lv != self._prev:
                off = rel % self.bit
                self.skew = max(self.skew, min(off, self.bit - off))
            if rel == len(self._samples) * self.bit + self.bit // 2:
                self._samples.append(lv)
                if len(self._samples) == self.nbits:
                    self._frame(t)
                    self._t0 = None
        self._prev = lv

    def _frame(self, t: int) -> None:
        s = self._samples
        data = s[1:1 + self.data_bits]
        if s[0] != 0:
            self.error(t, "start bit not 0 at its middle")
        if self.parity:
            ones = sum(data) + s[1 + self.data_bits]
            if ones % 2 != (0 if self.parity == "even" else 1):
                self.error(t, "parity")
        if any(b != 1 for b in s[-self.stop_bits:]):
            self.error(t, "stop bit not 1 (framing)")
        self.frames.append(sum(b << i for i, b in enumerate(data)))


class UartTx(Device):
    """UART transmitter: sends `data` frames from clock `start`, each followed by `gap` idle bit
    times. stop_low: send a 0 stop bit (framing error) for the frames whose index is in it."""

    def __init__(self, pin: int, bit: int, data: list[int], start: int = 0, gap: float = 0,
                 data_bits: int = 8, parity: str | None = None, stop_bits: int = 1,
                 stop_low: set[int] = frozenset()) -> None:
        super().__init__()
        self.pin = pin
        self.schedule: list[tuple[int, int]] = []      # (clock, level)
        t = start
        for i, value in enumerate(data):
            bits = [0] + [value >> k & 1 for k in range(data_bits)]
            if parity:
                bits.append((sum(bits[1:]) + (parity == "odd")) % 2)
            bits += [0 if i in stop_low else 1] * stop_bits
            for b in bits:
                self.schedule.append((t, b))
                t += bit
            self.schedule.append((t, 1))
            t += round(gap * bit)
        self.end = t
        self.drive = {pin: 1}

    def tick(self, t: int, levels: int) -> None:
        while self.schedule and self.schedule[0][0] <= t + 1:
            self.drive[self.pin] = self.schedule.pop(0)[1]


# ---------------------------------------------------------------------------------- SPI

class SpiSlave(Device):
    """SPI slave in mode 0..3 (CPOL = mode >> 1, CPHA = mode & 1), `bits` per word.

    Each word is 2 * bits SCK edges, the first one leading (away from CPOL). CPHA 0: data sampled
    on leading edges, changed on trailing ones (the first bit is out before the first edge);
    CPHA 1: changed on leading edges, sampled on trailing ones. With `cs`, the slave listens only
    while CS_N is low, and a word cut by CS_N rising is dropped. Checks: SCK at CPOL before every
    word (and when CS_N falls), MOSI stable for `setup` clocks before each sampling edge and on
    the clock after it."""

    def __init__(self, sck: int, mosi: int, miso: int, mode: int = 0, bits: int = 8,
                 reply: list[int] = (), msb_first: bool = True, cs: int | None = None,
                 setup: int = 1) -> None:
        super().__init__()
        self.setup = setup
        self._mosi_since = 0                             # clock of the last MOSI change
        self.sck, self.mosi, self.miso, self.cs = sck, mosi, miso, cs
        self._prev_cs = 1
        self.cpol, self.cpha, self.bits, self.msb_first = mode >> 1, mode & 1, bits, msb_first
        self.reply = list(reply)
        self.words: list[int] = []
        self._edge = 0                                   # edges seen in the current word
        self._shift = 0
        self._prev_sck: int | None = None
        self._prev_mosi = 0
        self._hold: int | None = None
        self._load()
        if self.cpha == 0:
            self._present()

    def _load(self) -> None:
        value = self.reply.pop(0) if self.reply else 0
        order = range(self.bits - 1, -1, -1) if self.msb_first else range(self.bits)
        self._out = [value >> i & 1 for i in order]

    def _present(self) -> None:
        self.drive = {self.miso: self._out.pop(0)}

    def tick(self, t: int, levels: int) -> None:
        sck, mosi = _bit(levels, self.sck), _bit(levels, self.mosi)
        if mosi != self._prev_mosi:
            self._mosi_since = t
        if self._hold is not None and mosi != self._hold:
            self.error(t, "MOSI changed on the clock after a sampling edge (hold)")
        self._hold = None
        if self.cs is not None:
            cs = _bit(levels, self.cs)
            if cs and not self._prev_cs and self._edge:
                self.error(t, "CS_N rose inside a word")
            if not cs and self._prev_cs and sck != self.cpol:
                self.error(t, f"SCK not at CPOL = {self.cpol} when CS_N falls")
            self._prev_cs = cs
            if cs:
                self._edge, self._shift = 0, 0
                self._prev_sck, self._prev_mosi = sck, mosi
                return
        if self._prev_sck is not None and sck != self._prev_sck:
            leading = self._prev_sck == self.cpol
            if self._edge == 0 and not leading:
                self.error(t, f"SCK not at CPOL = {self.cpol} before a word")
            if leading != bool(self.cpha):              # sampling edge
                if t - self._mosi_since < self.setup:
                    self.error(t, f"MOSI stable {t - self._mosi_since} clocks before the sampling edge (setup {self.setup})")
                self._hold = mosi
                n = self._edge // 2
                self._shift = (self._shift << 1 | mosi) if self.msb_first else self._shift | mosi << n
            else:                                       # shifting edge: next MISO bit
                if not self._out:
                    self._load()
                self._present()
            self._edge += 1
            if self._edge == 2 * self.bits:             # CPHA 0: the last (trailing) edge
                self.words.append(self._shift)          # already put out the next word's first bit
                self._edge, self._shift = 0, 0
        self._prev_sck, self._prev_mosi = sck, mosi


# ---------------------------------------------------------------------------------- I2C

class I2cTarget(Device):
    """I2C target (open drain: it only pulls SCL / SDA low) at a 7-bit `address`.

    Follows START, address + R/W, ACK, data bytes, master ACK / NACK on reads, repeated START and
    STOP. Writes are ACKed and stored; reads send `read_data`. stretch: clocks to hold SCL low
    after every byte (clock stretching). Checks: SDA never changes on a clock where SCL changes,
    and START / STOP only between bytes. `transactions` lists dicts addr, rw, data, acks."""

    def __init__(self, scl: int, sda: int, address: int, read_data: list[int] = (),
                 stretch: int = 0) -> None:
        super().__init__()
        self.scl, self.sda, self.address = scl, sda, address
        self.read_data = list(read_data)
        self.stretch = stretch
        self.transactions: list[dict] = []
        self._prev: tuple[int, int] | None = None
        self._state = "idle"                            # idle, addr, write, read, ignore
        self._bit = 0                                   # SCL rising edges in the current byte (9 = ACK clock)
        self._shift = 0
        self._tx: list[int] = []                        # bits still to send (reads)
        self._hold_scl = 0
        self._acking = False                            # this ACK clock is ours (address / write)

    def _sda(self, level: int | None) -> None:
        if level == 0:
            self.drive[self.sda] = 0
        else:
            self.drive.pop(self.sda, None)

    def tick(self, t: int, levels: int) -> None:
        scl, sda = _bit(levels, self.scl), _bit(levels, self.sda)
        if self._hold_scl:
            self._hold_scl -= 1
            if not self._hold_scl:
                self.drive.pop(self.scl, None)
        if self._prev is None:
            self._prev = scl, sda
            return
        pscl, psda = self._prev
        self._prev = scl, sda
        if scl != pscl and sda != psda:
            self.error(t, "SDA changed on the same clock as SCL")
            return
        if pscl == scl == 1 and sda != psda:            # START (falling) or STOP (rising)
            if self._state != "idle" and self._bit > 1:  # bit 1: SCL rose to start a byte or for P / Sr
                self.error(t, f"{'START' if not sda else 'STOP'} inside a byte")
            self._sda(None)
            if sda == 0:
                self.transactions.append({"addr": None, "rw": None, "data": [], "acks": []})
                self._state, self._bit, self._shift = "addr", 0, 0
            else:
                self._state = "idle"
            return
        if self._state == "idle":
            return
        cur = self.transactions[-1]
        if not pscl and scl:                            # rising edge: a bit is valid
            self._bit += 1
            if self._bit <= 8:
                self._shift = self._shift << 1 | sda
            elif self._state == "read" and not self._acking:   # master ACK / NACK of our byte
                cur["acks"].append(1 - sda)
                if sda:
                    self._state = "ignore"
        elif pscl and not scl:                          # falling edge: the next bit may change
            if self._bit == 8:                          # byte received / sent: ACK clock follows
                if self._state == "addr":
                    cur["addr"], cur["rw"] = self._shift >> 1, self._shift & 1
                    self._state = "write" if cur["addr"] == self.address else "ignore"
                    if self._state == "write":
                        self._sda(0)
                        self._acking = True
                        cur["acks"].append(1)
                        if cur["rw"]:
                            self._state = "read"
                elif self._state == "write":
                    cur["data"].append(self._shift)
                    cur["acks"].append(1)
                    self._sda(0)
                    self._acking = True
                elif self._state == "read":
                    self._sda(None)                     # master drives its ACK
                if self.stretch and self._state != "ignore":
                    self.drive[self.scl] = 0
                    self._hold_scl = self.stretch
            elif self._bit == 9:                        # end of the ACK clock
                self._bit, self._shift, self._acking = 0, 0, False
                self._sda(None)
                if self._state == "read":
                    value = self.read_data.pop(0) if self.read_data else 0xFF
                    cur["data"].append(value)
                    self._tx = [value >> i & 1 for i in range(7, -1, -1)]
                    self._sda(self._tx.pop(0))
            elif self._state == "read" and self._tx:
                self._sda(self._tx.pop(0))
