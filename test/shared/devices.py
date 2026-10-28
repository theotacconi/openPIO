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
    times (one number, or one per frame). stop_low: send a 0 stop bit (framing error) for the
    frames whose index is in it."""

    def __init__(self, pin: int, bit: float, data: list[int], start: int = 0,
                 gap: float | list[float] = 0, data_bits: int = 8, parity: str | None = None,
                 stop_bits: int = 1, stop_low: set[int] = frozenset()) -> None:
        super().__init__()
        self.pin = pin
        self.schedule: list[tuple[int, int]] = []      # (clock, level)
        gaps = gap if isinstance(gap, list) else [gap] * len(data)
        t = start
        for i, value in enumerate(data):
            bits = [0] + [value >> k & 1 for k in range(data_bits)]
            if parity:
                bits.append((sum(bits[1:]) + (parity == "odd")) % 2)
            bits += [0 if i in stop_low else 1] * stop_bits
            for b in bits:
                self.schedule.append((round(t), b))
                t += bit
            self.schedule.append((round(t), 1))
            t += gaps[i] * bit
        self.end = round(t)
        self.drive = {pin: 1}

    def tick(self, t: int, levels: int) -> None:
        while self.schedule and self.schedule[0][0] <= t + 1:
            self.drive[self.pin] = self.schedule.pop(0)[1]


def crc16_modbus(data: list[int]) -> int:
    """CRC-16/MODBUS: reflected polynomial 0x8005, init 0xFFFF; sent low byte first."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = crc >> 1 ^ 0xA001 if crc & 1 else crc >> 1
    return crc


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


class SpiMaster(Device):
    """SPI master, mode 0, MSB first, 8-bit words: each transfer of `transfers` is CS_N low,
    `lead` clocks, then per bit SCK `half` clocks low, `half` high (MOSI set as SCK falls, MISO
    sampled as it rises), then CS_N high for `gap` clocks. A word may be (value, bits) to end the
    transfer after its first bits. `received` gets each transfer's MISO words (whole ones).
    Checks: MISO stable for `setup` clocks before each rising edge."""

    def __init__(self, sck: int, mosi: int, miso: int, cs: int, transfers: list[list], half: int,
                 start: int = 0, lead: int = 0, gap: int = 0, setup: int = 1) -> None:
        super().__init__()
        self.miso, self.setup = miso, setup
        self.schedule: list[tuple[int, dict]] = []
        self.samples: list[tuple[int, int, int]] = []    # (clock, transfer, word)
        self.received: list[list[int]] = [[] for _ in transfers]
        t = start
        for n, words in enumerate(transfers):
            self.schedule.append((t, {cs: 0}))
            t += lead
            for k, w in enumerate(words):
                value, bits = w if isinstance(w, tuple) else (w, 8)
                for i in range(7, 7 - bits, -1):
                    self.schedule += [(t, {sck: 0, mosi: value >> i & 1}), (t + half, {sck: 1})]
                    if bits == 8:
                        self.samples.append((t + half, n, k))
                    t += 2 * half
            self.schedule += [(t, {sck: 0}), (t + half, {cs: 1})]
            t += half + gap
        self.end = t
        self.drive = {cs: 1, sck: 0, mosi: 0}
        self._miso, self._since = 0, 0

    def tick(self, t: int, levels: int) -> None:
        miso = _bit(levels, self.miso)
        if miso != self._miso:
            self._miso, self._since = miso, t
        while self.samples and self.samples[0][0] == t:
            _, n, k = self.samples.pop(0)
            if t - self._since < self.setup:
                self.error(t, f"MISO stable {t - self._since} clocks before SCK rose")
            words = self.received[n]
            if len(words) == k:
                words.append(0)
            words[k] = words[k] << 1 | miso
        while self.schedule and self.schedule[0][0] <= t + 1:
            self.drive.update(self.schedule.pop(0)[1])


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


# ---------------------------------------------------------------------------------- USB

J, K, SE0, SE1 = "J", "K", "0", "1"


def crc16_usb(data: list[int]) -> int:
    """CRC-16/USB of the data bytes (the value sent after them, LSB first)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def usb_states(pid: int, payload: list[int], crc: int | None = None) -> list[str]:
    """Line states, one per bit, of a low-speed packet: SYNC, PID, payload, CRC16, EOP, J."""
    crc = crc16_usb(payload) if crc is None else crc
    data = [0x80, pid] + payload + [crc & 0xFF, crc >> 8]
    bits = [b >> i & 1 for b in data for i in range(8)]
    states, level, ones = [], J, 0
    for b in bits:
        for bit in [b] + ([0] if b and ones == 5 else []):   # a stuffed 0 after six 1s
            ones = ones + 1 if bit else 0
            level = level if bit else (K if level == J else J)
            states.append(level)
    return states + [SE0, SE0, J]


class UsbTx(Device):
    """Low-speed USB transmitter on D- / D+ (J = D- high): sends `packets` [(pid, payload),
    (pid, payload, crc), or a list of line states] from clock `start`, `gap` idle bit times
    apart; idles at J."""

    def __init__(self, dm: int, dp: int, bit: int, packets: list[tuple], start: int = 0,
                 gap: int = 4) -> None:
        super().__init__()
        self.dm, self.dp = dm, dp
        self.schedule: list[tuple[int, str]] = []
        t = start
        for p in packets:
            for s in p if isinstance(p, list) else usb_states(*p):
                self.schedule.append((t, s))
                t += bit
            t += gap * bit
        self.end = t
        self._set(J)

    def _set(self, s: str) -> None:
        self.drive = {self.dm: int(s in (J, SE1)), self.dp: int(s in (K, SE1))}

    def tick(self, t: int, levels: int) -> None:
        while self.schedule and self.schedule[0][0] <= t + 1:
            self._set(self.schedule.pop(0)[1])


class UsbRx(Device):
    """Low-speed USB receiver on D- / D+: a packet starts at J -> K; each bit is sampled in the
    middle of its nominal time; NRZI, unstuffing (seven 1s is an error), then SE0 must last 2 bit
    times and be followed by J. `packets` gets dicts sync, pid, data (payload + CRC bytes), crc_ok;
    `skew` is the worst distance of an in-packet edge from a bit boundary."""

    def __init__(self, dm: int, dp: int, bit: int) -> None:
        super().__init__()
        self.dm, self.dp, self.bit = dm, dp, bit
        self.packets: list[dict] = []
        self.skew = 0
        self._prev = J
        self._t0: int | None = None

    def _state(self, levels: int) -> str:
        dm, dp = _bit(levels, self.dm), _bit(levels, self.dp)
        return {(1, 0): J, (0, 1): K, (0, 0): SE0}.get((dm, dp), "1")

    def tick(self, t: int, levels: int) -> None:
        s = self._state(levels)
        if self._t0 is None:
            if self._prev == J and s == K:
                self._t0, self._states, self._eop = t, [], None
        else:
            rel = t - self._t0
            if s != self._prev:
                off = rel % self.bit
                self.skew = max(self.skew, min(off, self.bit - off))
            if rel % self.bit == self.bit // 2:
                self._states.append(s)
                if s == "1":
                    self.error(t, "SE1 on the line")
                if self._states[-3:] == [SE0, SE0, J]:
                    self._packet(t)
                    self._t0 = None
                elif self._states[-3:] == [SE0, SE0, SE0]:
                    self.error(t, "SE0 longer than 2 bit times")
        self._prev = s

    def _packet(self, t: int) -> None:
        bits, level, ones = [], J, 0
        for s in self._states[:-3]:
            b = int(s == level)
            level = s
            if ones == 6:
                if b:
                    self.error(t, "bit stuffing error")
                ones = 0
                continue
            bits.append(b)
            ones = ones + 1 if b else 0
        if len(bits) % 8:
            self.error(t, f"{len(bits)} bits: not whole bytes")
        data = [sum(bits[i + k] << k for k in range(8)) for i in range(0, len(bits) - 7, 8)]
        body = data[2:]
        crc_ok = len(body) >= 2 and crc16_usb(body[:-2]) == body[-2] | body[-1] << 8
        self.packets.append({"sync": data[0] if data else None, "pid": data[1] if len(data) > 1 else None,
                             "data": body, "crc_ok": crc_ok})


# ---------------------------------------------------------------------------------- Manchester

class ManchesterRx(Device):
    """IEEE 802.3 Manchester receiver (1 = rising edge in the middle of the bit), idle low.

    A frame starts with a rising edge, the middle of its first bit (a 1). Bit k: middle at
    t0 + k * bit; its halves are sampled bit / 4 before and after; equal halves end the frame.
    `frames` gets the bit lists; every edge must fall on a half-bit boundary."""

    def __init__(self, pin: int, bit: int) -> None:
        super().__init__()
        self.pin, self.bit = pin, bit
        self.frames: list[list[int]] = []
        self._prev = 0
        self._t0: int | None = None
        self._first = 0

    def tick(self, t: int, levels: int) -> None:
        lv = _bit(levels, self.pin)
        if self._t0 is None:
            if self._prev == 0 and lv == 1:
                self._t0, self._bits = t, [1]
        else:
            rel = t - self._t0
            if lv != self._prev and rel % (self.bit // 2):
                self.error(t, "edge off the half-bit grid")
            phase = rel % self.bit
            if phase == self.bit - self.bit // 4:                 # first half of the next bit
                self._first = lv
            elif phase == self.bit // 4 and rel > self.bit // 2:  # second half
                if lv == self._first:
                    self.frames.append(self._bits)
                    self._t0 = None
                else:
                    self._bits.append(lv)
        self._prev = lv


def bits_to_bytes(bits: list[int]) -> list[int]:
    """LSB-first bytes of a bit list (the tail that is not a whole byte is dropped)."""
    return [sum(bits[i + k] << k for k in range(8)) for i in range(0, len(bits) - 7, 8)]


# ---------------------------------------------------------------------------------- CAN

def can_crc15(bits: list[int]) -> int:
    crc = 0
    for b in bits:
        fb = b ^ (crc >> 14 & 1)
        crc = (crc << 1) & 0x7FFF
        if fb:
            crc ^= 0x4599
    return crc


def can_bits(ident: int, data: list[int], rtr: int = 0) -> list[int]:
    """Unstuffed bits of a CAN 2.0A data frame from SOF to the end of the data field."""
    bits = [0] + [ident >> i & 1 for i in range(10, -1, -1)] + [rtr, 0, 0]
    bits += [len(data) >> i & 1 for i in range(3, -1, -1)]
    return bits + [b >> i & 1 for b in data for i in range(7, -1, -1)]


def can_stuff(bits: list[int]) -> list[int]:
    """After five equal bits, the complement (it counts for the next run)."""
    out, run, last = [], 0, None
    for b in bits:
        out.append(b)
        run = run + 1 if b == last else 1
        last = b
        if run == 5:
            out.append(1 - b)
            run, last = 1, 1 - b
    return out


def can_wire(ident: int, data: list[int]) -> list[int]:
    """Bits on the bus as sent: stuffed SOF..CRC, CRC delimiter, ACK slot (recessive), ACK
    delimiter, end of frame."""
    bits = can_bits(ident, data)
    crc = can_crc15(bits)
    return can_stuff(bits + [crc >> i & 1 for i in range(14, -1, -1)]) + [1, 1, 1] + [1] * 7


class CanNode:
    """Reference CAN node (not a pin device: WiredAndBus clocks it with the bus level).

    Receives every frame (unstuffing up to the CRC, which it checks) into `frames` as dicts id,
    data, crc_ok, and pulls the ACK slot dominant for good frames of others. Sends its `send`
    frames (id, data) after 11 idle bits, or joins another node's SOF (hard sync) with a frame
    pending, and stops at a lost arbitration; `sent` gets (id, acked). Bits are sampled in their
    middle, counted from the SOF edge; every bus edge of a frame must fall within a clock of a
    bit boundary (a transmitter's loop delay through its transceiver and input synchronizer).
    idle: recessive clocks already seen at start."""

    def __init__(self, bit: int, send: list[tuple[int, list[int]]] = (), ack: bool = True,
                 idle: int | None = None) -> None:
        self.bit, self.ack = bit, ack
        self.send = list(send)
        self.tx = 1
        self.frames: list[dict] = []
        self.sent: list[tuple[int, bool]] = []
        self.errors: list[str] = []
        self._idle = 11 * bit if idle is None else idle   # recessive clocks seen
        self._t0: int | None = None        # SOF start

    def step(self, t: int, bus: int) -> None:
        if self._t0 is None:
            self._idle = self._idle + 1 if bus else 0
            if not bus:                    # SOF of another node: listen, or join with a frame
                self._start(t, can_wire(*self.send[0]) if self.send else None)
            elif self.send and self._idle >= 11 * self.bit:
                self._start(t + 1, can_wire(*self.send[0]))
                self.tx = 0
            return
        k, phase = divmod(t - self._t0, self.bit)
        if bus != self._bus and 1 < phase < self.bit - 1:
            self.errors.append(f"t={t}: edge {phase} clocks into bit {k}")
        self._bus = bus
        if phase == self.bit - 1:          # drive bit k + 1 from the next clock
            nxt = k + 1
            if self._wire is not None and nxt < len(self._wire):
                self.tx = self._wire[nxt]
            else:
                self.tx = 0 if nxt == self._ack_bit else 1
        if phase == self.bit // 2:
            self._sample(t, k, bus)

    def _crc_end(self, k: int) -> None:
        """Bit k ends the stuffed region: the CRC delimiter is bit k + 1."""
        self._end = k + 1
        crc = sum(b << (14 - i) for i, b in enumerate(self._bits[-15:]))
        self._crc_ok = can_crc15(self._bits[:-15]) == crc
        if self._wire is None and self._crc_ok and self.ack:
            self._ack_bit = k + 2

    def _start(self, t0: int, wire: list[int] | None) -> None:
        self._t0, self._wire, self._bus = t0, wire, 0
        self._bits: list[int] = []         # unstuffed SOF .. CRC
        self._run, self._last = 0, None
        self._total = 0                    # unstuffed bits up to the end of the CRC
        self._end = None                   # bit index of the CRC delimiter
        self._ack_bit = None

    def _sample(self, t: int, k: int, bus: int) -> None:
        if self._wire is not None and k < len(self._wire) and self._wire[k] != bus:
            if self._end is None and len(self._bits) < 13 and self._wire[k] == 1:
                self._wire = None          # lost arbitration: go on as a receiver
                self.tx = 1
            elif k != (self._end or -2) + 1:   # the ACK slot is the receivers' to pull
                self.errors.append(f"t={t}: bit error")
        if self._end is None:              # stuffed region: SOF .. CRC
            if self._run == 5:             # a stuffed bit (also after the last CRC bit)
                if bus == self._last:
                    self.errors.append(f"t={t}: stuff error")
                self._run, self._last = 1, bus
                if len(self._bits) == self._total:
                    self._crc_end(k)
                return
            self._run = self._run + 1 if bus == self._last else 1
            self._last = bus
            self._bits.append(bus)
            if len(self._bits) == 19:
                dlc = sum(b << (3 - i) for i, b in enumerate(self._bits[15:19]))
                self._total = 19 + 8 * min(dlc, 8) + 15
            if len(self._bits) == self._total and self._run < 5:
                self._crc_end(k)
            return
        if k < self._end:
            return
        n = k - self._end                  # 0 CRC delimiter, 1 ACK slot, 2 ACK delimiter, 3.. EOF
        if n == 0 and not bus:
            self.errors.append(f"t={t}: CRC delimiter dominant")
        if n == 1 and self._wire is not None:
            self.sent.append((self.send.pop(0)[0], bus == 0))
        if n == 9:                         # last EOF bit
            b = self._bits
            data = [sum(x << (7 - i) for i, x in enumerate(b[19 + 8 * j:27 + 8 * j]))
                    for j in range((len(b) - 34) // 8)]
            self.frames.append({"id": sum(x << (10 - i) for i, x in enumerate(b[1:12])),
                                "data": data, "crc_ok": self._crc_ok})
            self._t0, self._idle = None, 0


class WiredAndBus(Device):
    """Wired-AND bus (dominant 0: CAN, LIN): the chip's TX pins and the reference nodes (objects
    with step(t, bus), tx and errors) drive it; the bus level goes to the chip's RX pins one clock
    later, as through a transceiver."""

    def __init__(self, tx_pins: list[int], rx_pins: list[int], nodes: list[CanNode]) -> None:
        super().__init__()
        self.tx_pins, self.rx_pins, self.nodes = tx_pins, rx_pins, nodes
        self.wave: list[int] = []
        self.drive = {p: 1 for p in rx_pins}

    def tick(self, t: int, levels: int) -> None:
        bus = min([_bit(levels, p) for p in self.tx_pins] + [n.tx for n in self.nodes])
        self.wave.append(bus)
        for n in self.nodes:
            n.step(t, bus)
        self.drive = {p: bus for p in self.rx_pins}
        self.errors = [e for n in self.nodes for e in n.errors]


# ---------------------------------------------------------------------------------- 1-Wire

def crc8_maxim(data: list[int]) -> int:
    crc = 0
    for byte in data:
        for i in range(8):
            fb = (crc ^ byte >> i) & 1
            crc >>= 1
            if fb:
                crc ^= 0x8C
    return crc


class OneWireDevice(Device):
    """1-Wire slave at standard speed (open drain), times in clocks of 1 us.

    Answers a reset (low >= 480) with a presence pulse (low 100 from 20 after the release).
    Every other low pulse starts a slot: the bit written is the level at 30 us. After a written
    byte found in `replies`, the device sends those bytes, LSB first, by holding the line low
    until 30 us in the slots of a 0. Checks: slot low times 1..15 (1, read) or 60..120 (0), slots
    of 60 us or more, at least 1 us of recovery. `written` lists the bytes received."""

    def __init__(self, pin: int, replies: dict[int, list[int]] = None, present: bool = True) -> None:
        super().__init__()
        self.pin, self.replies, self.present = pin, replies or {}, present
        self.written: list[int] = []
        self.resets = 0
        self._prev = 1
        self._fall = self._rise = None
        self._bits: list[int] = []
        self._send: list[int] = []
        self._release_at: int | None = None
        self._presence: tuple[int, int] | None = None
        self._own = False

    def tick(self, t: int, levels: int) -> None:
        lv = _bit(levels, self.pin)
        if self._presence and t == self._presence[0]:
            self.drive, self._fall = {self.pin: 0}, None          # our own low: not measured
        if self._presence and t == self._presence[1]:
            self.drive, self._presence = {}, None
        if self._release_at == t:
            self.drive, self._release_at = {}, None
        if self._prev == 1 and lv == 0 and not self.drive:          # master starts a slot / reset
            if self._fall is not None and t - self._fall < 60:
                self.error(t, f"slot of {t - self._fall} us")
            if self._rise is not None and t - self._rise < 1:
                self.error(t, "no recovery")
            self._fall = t
            if self._send:                                           # read slot: send a 0?
                bit = self._send[0]
                if not bit:                                          # hold 15 us: the shortest allowed
                    self.drive, self._release_at = {self.pin: 0}, t + 15
                    self._own = True
        if self._prev == 0 and lv == 1 and self._fall is not None and self._presence is None:
            low = t - self._fall
            self._rise = t
            if self._own:                                        # we held a read 0
                self._own = False
            elif low >= 480:
                self.resets += 1
                self._bits, self._send = [], []
                if self.present:
                    self._presence = (t + 20, t + 120)
            elif not (1 <= low <= 15 or 60 <= low <= 120):
                self.error(t, f"slot low for {low} us")
        if self._fall is not None and t == self._fall + 30 and self._presence is None:
            if self._send:                                           # a slot we sent in
                self._send.pop(0)
                self._prev = lv
                return
            self._bits.append(lv)
            if len(self._bits) == 8:
                value = sum(b << i for i, b in enumerate(self._bits))
                self._bits = []
                self.written.append(value)
                for byte in self.replies.get(value, []):
                    self._send += [byte >> i & 1 for i in range(8)]
        self._prev = lv


# ---------------------------------------------------------------------------------- JTAG

TAP_NEXT = {  # state: (next with TMS 0, next with TMS 1), IEEE 1149.1
    "TLR": ("RTI", "TLR"), "RTI": ("RTI", "SELDR"),
    "SELDR": ("CAPDR", "SELIR"), "CAPDR": ("SHDR", "EX1DR"), "SHDR": ("SHDR", "EX1DR"),
    "EX1DR": ("PAUDR", "UPDDR"), "PAUDR": ("PAUDR", "EX2DR"), "EX2DR": ("SHDR", "UPDDR"),
    "UPDDR": ("RTI", "SELDR"),
    "SELIR": ("CAPIR", "TLR"), "CAPIR": ("SHIR", "EX1IR"), "SHIR": ("SHIR", "EX1IR"),
    "EX1IR": ("PAUIR", "UPDIR"), "PAUIR": ("PAUIR", "EX2IR"), "EX2IR": ("SHIR", "UPDIR"),
    "UPDIR": ("RTI", "SELDR"),
}


class JtagTap(Device):
    """IEEE 1149.1 TAP with a 4-bit IR: IDCODE (0b0001, selected by Test-Logic-Reset), USER
    (0b1010, a 16-bit read / write register), BYPASS (0b1111). Registers shift LSB first; TMS and
    TDI are sampled on the TCK rising edge, TDO changes on the falling edge. Checks TMS / TDI
    stable on the clock before and after each rising edge."""

    IR_IDCODE, IR_USER, IR_BYPASS = 0b0001, 0b1010, 0b1111

    def __init__(self, tck: int, tms: int, tdi: int, tdo: int, idcode: int) -> None:
        super().__init__()
        self.tck, self.tms, self.tdi, self.tdo, self.idcode = tck, tms, tdi, tdo, idcode
        self.state, self.ir, self.user = "TLR", self.IR_IDCODE, 0
        self._sr, self._len = 0, 1
        self._prev: tuple[int, int, int] | None = None
        self._check: tuple[int, int] | None = None

    def _dr(self) -> tuple[int, int]:
        return {self.IR_IDCODE: (self.idcode, 32), self.IR_USER: (self.user, 16)}.get(self.ir, (0, 1))

    def tick(self, t: int, levels: int) -> None:
        tck, tms, tdi = _bit(levels, self.tck), _bit(levels, self.tms), _bit(levels, self.tdi)
        if self._check is not None and self._check != (tms, tdi):
            self.error(t, "TMS / TDI changed on the clock after a rising edge")
        self._check = None
        if self._prev and not self._prev[0] and tck:            # rising edge
            if self._prev[1:] != (tms, tdi):
                self.error(t, "TMS / TDI changed on a rising edge")
            self._check = (tms, tdi)
            s = self.state
            if s == "CAPDR":
                self._sr, self._len = self._dr()
            elif s == "CAPIR":
                self._sr, self._len = 0b0001, 4                  # IR capture value ..01
            elif s in ("SHDR", "SHIR"):
                self._sr = self._sr >> 1 | tdi << (self._len - 1)
            self.state = TAP_NEXT[s][tms]
            if self.state == "TLR":
                self.ir = self.IR_IDCODE
            if self.state == "UPDIR":
                self.ir = self._sr & 0xF
            if self.state == "UPDDR" and self.ir == self.IR_USER:
                self.user = self._sr & 0xFFFF
        if self._prev and self._prev[0] and not tck:             # falling edge
            self.drive = {self.tdo: self._sr & 1} if self.state in ("SHDR", "SHIR") else {}
        self._prev = (tck, tms, tdi)


# ---------------------------------------------------------------------------------- SWD

class SwdTarget(Device):
    """ARM SWD target (ADIv5): request sampled on SWCLK rising edges (start, APnDP, RnW, A[2:3],
    parity, stop, park), turnaround, ACK OK, then 32 data bits + even parity: sent for reads (the
    target changes SWDIO one clock after a rising edge), sampled on rising edges for writes, then
    turnaround. Zeros between requests are idle cycles. `regs` maps (APnDP, A[3:2]) to values;
    `log` lists (read / write, APnDP, addr, value). Checks request parity, start / stop / park,
    write parity."""

    def __init__(self, clk: int, dio: int, regs: dict | None = None) -> None:
        super().__init__()
        self.clk, self.dio = clk, dio
        self.regs = dict(regs or {})
        self.log: list[tuple] = []
        self._prev_clk = 0
        self._r = 0                       # rising edges in the current transaction (0: idle)
        self._req: list[int] = []
        self._data: list[int] = []
        self._out: list[int] = []

    def tick(self, t: int, levels: int) -> None:
        clk, dio = _bit(levels, self.clk), _bit(levels, self.dio)
        rising = clk and not self._prev_clk
        self._prev_clk = clk
        if not rising:
            return
        if self._r == 0 and not dio:
            return                        # idle cycle
        self._r += 1
        r = self._r
        if r <= 8:
            self._req.append(dio)
            if r == 8:
                q = self._req
                if (q[0], q[6], q[7]) != (1, 0, 1) or sum(q[1:5]) % 2 != q[5]:
                    self.error(t, f"bad request {q}")
                self._ap, self._rnw, self._addr = q[1], q[2], q[3] << 2 | q[4] << 3
        elif r <= 11:                     # turnaround, then ACK OK = 1, 0, 0 (LSB first)
            self.drive = {self.dio: [1, 0, 0][r - 9]}
        elif self._rnw:                   # read: data after 12..43, parity after 44, release at 45
            if r == 12:
                v = self.regs.get((self._ap, self._addr), 0)
                self._out = [v >> i & 1 for i in range(32)] + [bin(v).count("1") % 2]
                self.log.append(("r", self._ap, self._addr, v))
            if r <= 44:
                self.drive = {self.dio: self._out[r - 12]}
            else:
                self._end()
        else:                             # write: release at 12, turnaround 13, data 14..45, parity 46
            self.drive = {}
            if 14 <= r <= 46:
                self._data.append(dio)
            if r == 46:
                v = sum(b << i for i, b in enumerate(self._data[:32]))
                if bin(v).count("1") % 2 != self._data[32]:
                    self.error(t, "write parity")
                self.regs[(self._ap, self._addr)] = v
                self.log.append(("w", self._ap, self._addr, v))
                self._end()

    def _end(self) -> None:
        self.drive, self._r, self._req, self._data = {}, 0, [], []


class BmcRx(Device):
    """Biphase mark receiver: a transition at every bit boundary, one more in the middle of a 1.
    The first edge on an idle line starts a frame; bit k spans t0 + k * bit; its halves are
    sampled at a quarter and three quarters; no transition at a boundary ends the frame."""

    def __init__(self, pin: int, bit: int) -> None:
        super().__init__()
        self.pin, self.bit = pin, bit
        self.frames: list[list[int]] = []
        self._prev = 0
        self._t0: int | None = None

    def tick(self, t: int, levels: int) -> None:
        lv = _bit(levels, self.pin)
        if self._t0 is None:
            if lv != self._prev:
                self._t0, self._bits, self._last = t, [], self._prev
        else:
            rel = t - self._t0
            if lv != self._prev and rel % (self.bit // 2):
                self.error(t, "edge off the half-bit grid")
            phase = rel % self.bit
            if phase == self.bit // 4:
                if lv == self._last:              # no boundary transition: end of frame
                    self.frames.append(self._bits)
                    self._t0 = None
                else:
                    self._first = lv
            elif phase == 3 * self.bit // 4:
                self._bits.append(int(lv != self._first))
                self._last = lv
        self._prev = lv


class BmcTx(Device):
    """Biphase mark transmitter: sends the `frames` (bit lists) from clock `start`, `gap` bit
    times apart: a transition at every bit boundary, one more in the middle of a 1; between
    frames the line holds its level. `bit` may be fractional (a fast or slow transmitter)."""

    def __init__(self, pin: int, bit: float, frames: list[list[int]], start: int = 0,
                 gap: float = 8) -> None:
        super().__init__()
        self.pin = pin
        self.schedule: list[tuple[int, int]] = []
        t, level = start, 0
        for bits in frames:
            for b in bits:
                level ^= 1
                self.schedule.append((round(t), level))
                if b:
                    level ^= 1
                    self.schedule.append((round(t + bit / 2), level))
                t += bit
            t += gap * bit
        self.end = round(t)
        self.drive = {pin: 0}

    def tick(self, t: int, levels: int) -> None:
        while self.schedule and self.schedule[0][0] <= t + 1:
            self.drive = {self.pin: self.schedule.pop(0)[1]}


# ---------------------------------------------------------------------------------- LIN

def lin_pid(ident: int) -> int:
    """Protected identifier: 6-bit ID with parity P0 = ID0^ID1^ID2^ID4, P1 = !(ID1^ID3^ID4^ID5)."""
    b = [ident >> i & 1 for i in range(6)]
    return ident | (b[0] ^ b[1] ^ b[2] ^ b[4]) << 6 | (1 - (b[1] ^ b[3] ^ b[4] ^ b[5])) << 7


def lin_checksum(pid: int, data: list[int]) -> int:
    """Enhanced checksum (LIN 2.x): inverted 8-bit sum with carry of PID and data."""
    s = pid
    for d in data:
        s += d
        s = (s & 0xFF) + (s >> 8)
    return ~s & 0xFF


class LinSlave:
    """Reference LIN slave (a node of WiredAndBus). Detects a break (11 dominant bits or more;
    the master must send 13), then decodes UART bytes in the middle of their bits: sync (0x55),
    protected ID (parity checked). For an ID in `publish` it sends the data and checksum one bit
    after the ID; for an ID in `subscribe` (data length) it receives the master's data and checks
    the checksum. `frames` lists (ID, data, checksum ok)."""

    def __init__(self, bit: int, publish: dict[int, list[int]] = None,
                 subscribe: dict[int, int] = None) -> None:
        self.bit = bit
        self.publish, self.subscribe = publish or {}, subscribe or {}
        self.tx = 1
        self.frames: list[tuple[int, list[int], bool]] = []
        self.errors: list[str] = []
        self._low = 0                     # dominant clocks in a row
        self._prev = 1
        self._state = "idle"              # idle -> bytes after a break
        self._t0: int | None = None       # start bit of the byte being received
        self._bytes: list[int] = []
        self._send: list[tuple[int, int]] = []   # (clock, level)

    def step(self, t: int, bus: int) -> None:
        while self._send and self._send[0][0] <= t + 1:
            self.tx = self._send.pop(0)[1]
        if not bus:
            self._low += 1
        else:
            if self._low >= 11 * self.bit:    # a break just ended
                if self._low < 13 * self.bit:
                    self.errors.append(f"t={t}: break of {self._low} clocks, under 13 bits")
                self._state, self._bytes, self._t0 = "bytes", [], None
            self._low = 0
        if self._state == "bytes":
            if self._t0 is None:
                if self._prev == 1 and bus == 0:
                    self._t0, self._samples = t, []
            elif (t - self._t0) % self.bit == self.bit // 2:
                self._samples.append(bus)
                if len(self._samples) == 10:
                    self._byte(t)
                    self._t0 = None
        self._prev = bus

    def _byte(self, t: int) -> None:
        s = self._samples
        if s[0] != 0 or s[9] != 1:
            self.errors.append(f"t={t}: framing")
        self._bytes.append(sum(b << i for i, b in enumerate(s[1:9])))
        b = self._bytes
        if len(b) == 1 and b[0] != 0x55:
            self.errors.append(f"t={t}: sync {b[0]:#x}")
        if len(b) == 2:
            ident = b[1] & 0x3F
            if lin_pid(ident) != b[1]:
                self.errors.append(f"t={t}: PID parity {b[1]:#x}")
            if ident in self.publish:     # respond one bit after the PID's stop bit
                data = self.publish[ident]
                start = self._t0 + 10 * self.bit + self.bit
                for byte in data + [lin_checksum(b[1], data)]:
                    for lv in [0] + [byte >> i & 1 for i in range(8)] + [1]:
                        self._send.append((start, lv))
                        start += self.bit
                self._expect = len(data) + 1
            else:
                self._expect = self.subscribe.get(ident, 0) + 1
        if len(b) > 2 and len(b) == 2 + self._expect:
            data, chk = b[2:-1], b[-1]
            self.frames.append((b[1] & 0x3F, data, chk == lin_checksum(b[1], data)))
            self._state = "idle"


# ---------------------------------------------------------------------------------- PS/2

def ps2_bits(byte: int) -> list[int]:
    """Start, 8 data bits LSB first, odd parity, stop."""
    data = [byte >> i & 1 for i in range(8)]
    return [0] + data + [1 - sum(data) % 2, 1]


class Ps2Device(Device):
    """PS/2 keyboard (open drain on CLK and DATA, times in clocks of 1 us; clock period 80).

    Sends the `keys` bytes: DATA set with CLK high, CLK low from 20 to 60 in each bit, so the host
    samples on the falling edge. Takes host commands: after the host held CLK low 100 us or more
    and releases it with DATA low, it clocks 11 pulses, samples the data, parity and stop bits on
    the rising edges, pulls DATA low in the 11th (ACK), checks odd parity and the stop bit, and
    answers 0xFA. `received` lists the command bytes. bad_parity: indices of keys sent with a
    wrong parity bit."""

    def __init__(self, clk: int, data: int, keys: list[int] = (), start: int = 50,
                 bad_parity: set[int] = frozenset()) -> None:
        super().__init__()
        self.clk, self.data = clk, data
        self.received: list[int] = []
        self._out: list[tuple[int, dict]] = []   # (clock, drive)
        self._t = start
        for i, k in enumerate(keys):
            self._frame(k, flip=i in bad_parity)
        self._low = 0                            # clocks the host holds CLK low
        self._prev = (1, 1)
        self._rx: list[int] | None = None        # host bits being received
        self._clocking = False                   # we clock the host's bits

    def _frame(self, byte: int, gap: int = 200, flip: bool = False) -> None:
        t = max(self._t, self._out[-1][0] + gap if self._out else self._t)
        bits = ps2_bits(byte)
        bits[9] ^= flip
        for b in bits:
            dat = {} if b else {self.data: 0}
            self._out += [(t, dict(dat)), (t + 20, {self.clk: 0, **dat}), (t + 60, dict(dat))]
            t += 80
        self._out.append((t, {}))
        self._t = t

    def tick(self, t: int, levels: int) -> None:
        clk, dat = _bit(levels, self.clk), _bit(levels, self.data)
        driving_clk = self.clk in self.drive
        rising = clk and not self._prev[0]
        if self._rx is not None and rising and self._clocking:
            self._rx.append(dat)                   # our rising edge: sample the host's bit
            if len(self._rx) == 10:
                data, par, stop = self._rx[:8], self._rx[8], self._rx[9]
                if (sum(data) + par) % 2 != 1:
                    self.error(t, "command parity")
                if stop != 1:
                    self.error(t, "command stop bit")
                self.received.append(sum(b << i for i, b in enumerate(data)))
                self._rx, self._clocking = None, False
                self._t = t + 300
                self._frame(0xFA)                  # acknowledge the command
        if not clk and not driving_clk:
            self._low += 1
        elif clk and self._low >= 100 and not dat and self._rx is None:   # request to send
            self._rx = []
            self._out = [o for o in self._out if o[0] <= t]                  # stop sending
            start = t + 15
            for k in range(11):                    # 10 bits + ACK pulse
                ack = {self.data: 0} if k == 10 else {}
                self._out += [(start + 80 * k, {self.clk: 0, **ack}), (start + 80 * k + 40, dict(ack))]
            self._out.append((start + 880, {}))
            self._low = 0
        elif clk:
            self._low = 0
        if driving_clk and self._rx is not None:
            self._clocking = True                  # from our first clock pulse on
        while self._out and self._out[0][0] <= t + 1:
            self.drive = self._out.pop(0)[1]
        self._prev = (clk, dat)


# ---------------------------------------------------------------------------------- WS2812

class Ws2812Chain(Device):
    """A chain of `n` WS2812B LEDs on `pin`, `ns` nanoseconds per clock. Datasheet timing, each
    +-150 ns: a 0 is high 400 then low 850, a 1 high 800 then low 450; low for more than 50 us
    latches. After a latch the first LED takes the first 24 bits, the next LED the next 24 (each
    passes on what it does not take), G, R, B, MSB first. `frames` gets the colours (G, R, B)
    of the chain at every latch."""

    def __init__(self, pin: int, n: int, ns: float) -> None:
        super().__init__()
        self.pin, self.n, self.ns = pin, n, ns
        self.frames: list[list[tuple[int, int, int]]] = []
        self._leds = [(0, 0, 0)] * n
        self._bits: list[int] = []
        self._prev, self._rise, self._fall = 0, None, 0

    def _check(self, t: int, what: str, clocks: int, nominal: int) -> None:
        if abs(clocks * self.ns - nominal) > 150:
            self.error(t, f"{what} {clocks * self.ns:.0f} ns, not {nominal} +- 150")

    def tick(self, t: int, levels: int) -> None:
        lv = _bit(levels, self.pin)
        if lv and not self._prev:
            if self._rise is not None and self._bits and (t - self._fall) * self.ns <= 50_000:
                self._check(t, "low", t - self._fall, 850 if self._bits[-1] == 0 else 450)
            self._rise = t
        elif not lv and self._prev and self._rise is not None:
            high = (t - self._rise) * self.ns
            bit = int(high > 600)
            self._check(t, "high", t - self._rise, 800 if bit else 400)
            self._bits.append(bit)
            self._fall = t
        elif not lv and self._bits and (t - self._fall) * self.ns > 50_000:
            words = [sum(b << (23 - i) for i, b in enumerate(self._bits[k:k + 24]))
                     for k in range(0, min(len(self._bits), 24 * self.n) - 23, 24)]
            for i, w in enumerate(words):
                self._leds[i] = (w >> 16, w >> 8 & 0xFF, w & 0xFF)
            self.frames.append(list(self._leds))
            self._bits = []
        self._prev = lv


# ---------------------------------------------------------------------------------- I2S

class I2sCodec(Device):
    """I2S (Philips) codec, 16-bit samples, MSB first, LRCLK low = left. Its DAC samples `sdin`
    on BCLK rising edges; a word starts one BCLK after LRCLK changes, so the bit sampled on the
    edge where the change is seen is the LSB of the word before. Its ADC drives `sdout` the same
    way, changing it after BCLK falling edges, with the samples of `send` [(left, right), ...]
    (zeros after them). `received` gets (left, right) pairs. Checks: 16 bits per word, SDIN
    stable for `setup` clocks before a rising edge."""

    def __init__(self, bclk: int, lrclk: int, sdin: int, sdout: int, send=(), setup: int = 2):
        super().__init__()
        self.bclk, self.lrclk, self.sdin, self.sdout, self.setup = bclk, lrclk, sdin, sdout, setup
        self.send = [s for pair in send for s in pair]
        self.received: list[tuple[int, int]] = []
        self._words: list[tuple[int, int]] = []          # (channel, value)
        self._bits: list[int] | None = None
        self._out: list[int] = []
        self._lr: int | None = None
        self._prev = (0, 0)
        self._sd_since = 0
        self.drive = {sdout: 0}

    def tick(self, t: int, levels: int) -> None:
        bclk, sd = _bit(levels, self.bclk), _bit(levels, self.sdin)
        if sd != self._prev[1]:
            self._sd_since = t
        if bclk and not self._prev[0]:                   # rising edge: sample
            lr = _bit(levels, self.lrclk)
            if t - self._sd_since < self.setup and self._bits is not None:
                self.error(t, "SDIN changed less than setup clocks before BCLK rose")
            if self._bits is not None:
                self._bits.append(sd)
            if self._lr is not None and lr != self._lr:
                if self._bits is not None:
                    if len(self._bits) != 16:
                        self.error(t, f"{len(self._bits)} bits in a word")
                    self._word(1 - lr, sum(b << (len(self._bits) - 1 - i) for i, b in enumerate(self._bits)))
                self._bits = []
                value = self.send.pop(0) if self.send else 0
                self._out = [value >> i & 1 for i in range(15, -1, -1)]
            self._lr = lr
        elif not bclk and self._prev[0] and self._out:   # falling edge: next ADC bit
            self.drive = {self.sdout: self._out.pop(0)}
        self._prev = (bclk, sd)

    def _word(self, channel: int, value: int) -> None:
        if channel == 1 and self._words and self._words[-1][0] == 0:
            self.received.append((self._words.pop()[1], value))
        elif channel == 0:
            self._words = [(0, value)]


# ---------------------------------------------------------------------------------- quadrature

class QuadratureEncoder(Device):
    """Incremental encoder on pins `a`, `b`: from clock `start`, one step every `period` clocks,
    +1 or -1 from `steps`. +1 moves along (A, B) 00 -> 10 -> 11 -> 01 -> 00 (A leads), -1
    back. `position` is the sum of the steps taken so far."""

    GRAY = [(0, 0), (1, 0), (1, 1), (0, 1)]

    def __init__(self, a: int, b: int, steps: list[int], period: int, start: int = 0) -> None:
        super().__init__()
        self.a, self.b = a, b
        self.schedule = [(start + k * period, s) for k, s in enumerate(steps)]
        self.end = start + len(steps) * period
        self.position = 0
        self.drive = {a: 0, b: 0}

    def tick(self, t: int, levels: int) -> None:
        while self.schedule and self.schedule[0][0] <= t + 1:
            self.position += self.schedule.pop(0)[1]
            a, b = self.GRAY[self.position % 4]
            self.drive = {self.a: a, self.b: b}


# ---------------------------------------------------------------------------------- NEC IR

def nec_levels(frame) -> list[tuple[int, float]]:
    """(level, microseconds) of an IR receiver's output (0 during a burst) for one NEC frame:
    (address, command), or "repeat"."""
    if frame == "repeat":
        return [(0, 9000), (1, 2250), (0, 562.5)]
    address, command = frame
    data = [address, address ^ 0xFF, command, command ^ 0xFF]
    out = [(0, 9000), (1, 4500)]
    for byte in data:
        for i in range(8):
            out += [(0, 562.5), (1, 1687.5 if byte >> i & 1 else 562.5)]
    return out + [(0, 562.5)]


class NecRemote(Device):
    """Output of an IR receiver module (active low, idle high) for NEC remote frames sent from
    clock `start`, `gap` microseconds apart; `us` clocks per microsecond. A frame is
    (address, command), "repeat", or ("glitch", length in us): a lone burst."""

    def __init__(self, pin: int, frames: list, us: float = 1, start: int = 0, gap: float = 5000):
        super().__init__()
        self.pin = pin
        self.schedule: list[tuple[int, int]] = []
        t = start
        for f in frames:
            levels = [(0, f[1])] if f[0] == "glitch" else nec_levels(f)
            for lv, length in levels:
                self.schedule.append((round(t), lv))
                t += length * us
            self.schedule.append((round(t), 1))
            t += gap * us
        self.end = round(t)
        self.drive = {pin: 1}

    def tick(self, t: int, levels: int) -> None:
        while self.schedule and self.schedule[0][0] <= t + 1:
            self.drive = {self.pin: self.schedule.pop(0)[1]}


# ---------------------------------------------------------------------------------- HDLC

def crc16_x25(data: list[int]) -> int:
    """CRC-16/X.25 (the HDLC FCS): reflected polynomial 0x1021, init and xorout 0xFFFF."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = crc >> 1 ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


class HdlcRx(Device):
    """HDLC receiver on an NRZI line (a 0 is a transition, as AX.25), `bit` clocks a bit: every
    edge re-aligns the sampling grid to the middle of the bit. Flags 0x7E delimit frames; a 0
    after five 1s is removed; bytes LSB first; the last two bytes are the FCS (CRC-16/X.25, low
    byte first). `frames` gets dicts data (without FCS) and fcs_ok. Checks: whole bytes, no
    abort (seven 1s) inside a frame, edges on the bit grid inside a frame (between frames it
    re-aligns, as a receiver's clock recovery does)."""

    def __init__(self, pin: int, bit: int) -> None:
        super().__init__()
        self.pin, self.bit = pin, bit
        self.frames: list[dict] = []
        self._prev = self._last = 0
        self._next: int | None = None
        self._bits: list[int] | None = None              # None: hunting for a flag
        self._ones = 0

    def tick(self, t: int, levels: int) -> None:
        lv = _bit(levels, self.pin)
        if lv != self._prev:
            if self._bits and (self._next - t) % self.bit != self.bit // 2:
                self.error(t, "edge off the bit grid inside a frame")
            if self._next is None:
                self._last = self._prev
            self._next = t + self.bit // 2
        if t == self._next:
            self._take(t, int(lv == self._last))
            self._last = lv
            self._next += self.bit
        self._prev = lv

    def _take(self, t: int, b: int) -> None:
        if self._ones == 5 and not b:                    # stuffed 0
            self._ones = 0
            return
        if self._ones == 6:
            if not b:                                    # flag 01111110
                if self._bits:
                    self._frame(t, self._bits[:-6])
                self._bits, self._ones = [], 0
                return
            if self._bits and len(self._bits) > 6:
                self.error(t, "abort (seven 1s) inside a frame")
            self._bits = None                            # idle: hunt
            return
        self._ones = self._ones + 1 if b else 0
        if self._bits is not None and self._ones < 6:
            self._bits.append(b)

    def _frame(self, t: int, bits: list[int]) -> None:
        if not bits:
            return
        if len(bits) % 8:
            self.error(t, f"{len(bits)} bits: not whole bytes")
        data = [sum(bits[i + k] << k for k in range(8)) for i in range(0, len(bits) - 7, 8)]
        body, fcs = data[:-2], data[-2:]
        self.frames.append({"data": body, "fcs_ok": len(fcs) == 2 and crc16_x25(body) == fcs[0] | fcs[1] << 8})


# ---------------------------------------------------------------------------------- DALI

class DaliGear:
    """DALI control gear (IEC 62386-101; not a pin device: DaliBus clocks it with the bus level).
    Manchester with half-bit `te` clocks, 1 = the bus rising in the middle of the bit, idle high.
    A frame starts at a falling edge on the idle bus; each half of bit k is sampled in its middle;
    two equal halves are the stop condition. Forward frames (start bit and 16 bits, MSB first) go
    to `frames`; frames of 9 bits (backward frames of other gear) are ignored. A forward frame in
    `answers` is answered `settle` clocks after its last bit with a backward frame: start bit and
    8 bits. Checks: start bit 1, frame length, every edge within te / 8 of the half-bit grid."""

    def __init__(self, te: int, answers: dict[int, int] = None, settle: int = 0) -> None:
        self.te, self.answers, self.settle = te, dict(answers or {}), settle
        self.tx = 1
        self.frames: list[int] = []
        self.errors: list[str] = []
        self._prev = 1
        self._t0: int | None = None
        self._send: list[tuple[int, int]] = []           # (clock, level) of a backward frame

    def step(self, t: int, bus: int) -> None:
        if self._send:
            while self._send and self._send[0][0] <= t + 1:
                self.tx = self._send.pop(0)[1]
            self._prev = 1
            return
        if self._t0 is None:
            if self._prev and not bus:
                self._t0, self._bits = t, []
        else:
            rel, te = t - self._t0, self.te
            if bus != self._prev and min(rel % te, te - rel % te) > te // 8:
                self.errors.append(f"t={t}: edge {rel % te} clocks off the half-bit grid")
            k, phase = divmod(rel, 2 * te)
            if phase == te // 2:
                self._first = bus
            elif phase == te + te // 2:
                if bus != self._first:
                    self._bits.append(bus)
                else:
                    self._frame(self._t0 + 2 * te * k)
        self._prev = bus

    def _frame(self, end: int) -> None:
        bits, self._t0 = self._bits, None
        if len(bits) == 9:
            return
        if len(bits) != 17 or bits[0] != 1:
            self.errors.append(f"t={end}: frame of {len(bits)} bits, start bit {bits[:1]}")
            return
        frame = sum(b << (15 - i) for i, b in enumerate(bits[1:]))
        self.frames.append(frame)
        if frame in self.answers:
            t = end + self.settle
            for b in [1] + [self.answers[frame] >> i & 1 for i in range(7, -1, -1)]:
                self._send += [(t, 1 - b), (t + self.te, b)]
                t += 2 * self.te
            self._send.append((t, 1))


class DaliBus(Device):
    """DALI bus through the master's inverting transceiver: pin `tx` high pulls the bus low,
    pin `rx` reads the bus inverted. The gear (DaliGear) pull it low too; the bus is high
    otherwise (the bus power supply)."""

    def __init__(self, tx: int, rx: int, gear: list[DaliGear]) -> None:
        super().__init__()
        self.tx, self.rx, self.gear = tx, rx, gear
        self.wave: list[int] = []
        self.drive = {rx: 0}

    def tick(self, t: int, levels: int) -> None:
        bus = int(not _bit(levels, self.tx) and all(g.tx for g in self.gear))
        self.wave.append(bus)
        for g in self.gear:
            g.step(t, bus)
        self.drive = {self.rx: 1 - bus}
        self.errors = [e for g in self.gear for e in g.errors]
