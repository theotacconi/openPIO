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


# ---------------------------------------------------------------------------------- USB

J, K, SE0 = "J", "K", "0"


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
    """Low-speed USB transmitter on D- / D+ (J = D- high): sends `packets` [(pid, payload) or
    (pid, payload, crc)] from clock `start`, `gap` idle bit times apart; idles at J."""

    def __init__(self, dm: int, dp: int, bit: int, packets: list[tuple], start: int = 0,
                 gap: int = 4) -> None:
        super().__init__()
        self.dm, self.dp = dm, dp
        self.schedule: list[tuple[int, str]] = []
        t = start
        for p in packets:
            for s in usb_states(*p):
                self.schedule.append((t, s))
                t += bit
            t += gap * bit
        self.end = t
        self._set(J)

    def _set(self, s: str) -> None:
        self.drive = {self.dm: int(s == J), self.dp: int(s == K)}

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
    """Reference CAN node (not a pin device: CanBus clocks it with the bus level).

    Receives every frame (unstuffing up to the CRC, which it checks) into `frames` as dicts id,
    data, crc_ok, and pulls the ACK slot dominant for good frames of others. Sends its `send`
    frames (id, data) after 11 idle bits, or joins another node's SOF (hard sync) with a frame
    pending, and stops at a lost arbitration; `sent` gets (id, acked). Bits are sampled in their
    middle, counted from the SOF edge. idle: recessive clocks already seen at start."""

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
        if phase == self.bit - 1:          # drive bit k + 1 from the next clock
            nxt = k + 1
            if self._wire is not None and nxt < len(self._wire):
                self.tx = self._wire[nxt]
            else:
                self.tx = 0 if nxt == self._ack_bit else 1
        if phase == self.bit // 2:
            self._sample(t, k, bus)

    def _start(self, t0: int, wire: list[int] | None) -> None:
        self._t0, self._wire = t0, wire
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
            if self._run == 5:             # a stuffed bit
                if bus == self._last:
                    self.errors.append(f"t={t}: stuff error")
                self._run, self._last = 1, bus
                return
            self._run = self._run + 1 if bus == self._last else 1
            self._last = bus
            self._bits.append(bus)
            if len(self._bits) == 19:
                dlc = sum(b << (3 - i) for i, b in enumerate(self._bits[15:19]))
                self._total = 19 + 8 * min(dlc, 8) + 15
            if len(self._bits) == self._total:
                self._end = k + 1
                crc = sum(b << (14 - i) for i, b in enumerate(self._bits[-15:]))
                self._crc_ok = can_crc15(self._bits[:-15]) == crc
                if self._wire is None and self._crc_ok and self.ack:
                    self._ack_bit = k + 2
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


class CanBus(Device):
    """Wired-AND CAN bus (dominant 0): the chip's TX pins and the reference nodes drive it; the
    bus level goes to the chip's RX pins one clock later, as through a transceiver."""

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
