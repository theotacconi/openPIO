"""sigrok.py - a program judged by sigrok's protocol decoders, as a logic analyzer would judge
the chip on a board.

The decoders of sigrok (https://sigrok.org, run through sigrok-cli) are written by the sigrok
project from the protocol specifications and used with real analyzers on real devices; they
never saw openPIO, its model or its reference devices. A test records the pins in its Bench (one
sample a clock), writes them to a sigrok session file, runs sigrok-cli on it and compares what
the decoder read with what the host sent or received.

    rows = decode(bench, {"rx": 0}, "uart:baudrate=115200", "uart=rx-data", clock_hz=50e6)
    data = [int(text, 16) for _, _, text in rows]

sigrok-cli is needed (Fedora and Ubuntu: the sigrok-cli package); without it the test fails and
says so, it is not skipped.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path


def session(path: Path, trace: list[int], pins: dict[str, int], clock_hz: float) -> None:
    """A sigrok session file (.sr, as PulseView saves one): one channel per entry of pins,
    named after it, one sample per clock of trace."""
    names = list(pins)
    if len(names) > 8:
        raise ValueError("at most 8 channels")
    lut: dict[int, int] = {}
    data = bytearray(len(trace))
    for i, levels in enumerate(trace):
        b = lut.get(levels)
        if b is None:
            b = lut[levels] = sum((levels >> pins[n] & 1) << k for k, n in enumerate(names))
        data[i] = b
    meta = ["[global]", "sigrok version=0.5.2", "", "[device 1]", "capturefile=logic-1",
            f"total probes={len(names)}", f"samplerate={int(round(clock_hz))} Hz",
            "total analog=0", *(f"probe{k + 1}={n}" for k, n in enumerate(names)), "unitsize=1", ""]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("version", "2")
        z.writestr("metadata", "\n".join(meta))
        z.writestr("logic-1-1", bytes(data))


def decode(bench, pins: dict[str, int], decoders: str, annotations: str, clock_hz: float,
           start: int = 0, end: int | None = None, lead: int = 0, tail_edge: int | None = None
           ) -> list[tuple[int, int, str]]:
    """Run sigrok's decoders on the pins of bench (clocks start..end) and return the annotations
    asked for, as (first sample, last sample, text), samples counted from `start`.

    pins: the first decoder's channel name -> chip pin. decoders: a sigrok-cli stack, e.g.
    "uart:baudrate=9600,modbus". annotations: sigrok-cli's -A, e.g. "uart=rx-data". clock_hz:
    the chip's clock, the sample rate (the decoders that measure time need the real one).
    lead: samples of the first levels put before the recording, the line at rest before the
    chip started, for decoders that must see it idle first (DALI); the annotations count them.
    tail_edge: a pin that changes level once more, 100 samples after the recording, for
    decoders that report a packet only at the next edge (USB PD)."""
    cli = shutil.which("sigrok-cli")
    if cli is None:
        raise RuntimeError("sigrok-cli not found: install the sigrok-cli package "
                           "(the protocol tests use sigrok's decoders as an independent judge)")
    stack = []
    for k, part in enumerate(decoders.split(",")):
        name, *opts = part.split(":")
        chans = [f"{c}={c}" for c in pins] if k == 0 else []
        stack.append(":".join([name, *chans, *opts]))
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "pins.sr"
        trace = bench.trace[start:end]
        trace = [trace[0]] * lead + trace
        if tail_edge is not None:
            trace = trace + [trace[-1]] * 100 + [trace[-1] ^ 1 << tail_edge] * 100
        session(path, trace, pins, clock_hz)
        # Python 3.12 warns about old escape sequences in some decoders' sources (not ours)
        env = {**os.environ, "PYTHONWARNINGS": "ignore::SyntaxWarning"}
        run = subprocess.run([cli, "-i", str(path), "-P", ",".join(stack), "-A", annotations,
                              "--protocol-decoder-samplenum"],
                             capture_output=True, text=True, check=False, env=env)
    if run.returncode or run.stderr.strip():
        raise RuntimeError(f"sigrok-cli {decoders}: {run.stderr.strip() or run.returncode}")
    rows = []
    for line in run.stdout.splitlines():
        span, _, text = line.partition(" ")
        a, _, b = span.partition("-")
        rows.append((int(a), int(b), text.split(": ", 1)[1] if ": " in text else text))
    return rows


def texts(rows: list[tuple[int, int, str]]) -> list[str]:
    return [t for _, _, t in rows]


# ------------------------------------------------------------------------- per protocol

def uart(bench, pin: int, bit: float, warnings: bool = False, **opts) -> list:
    """The bytes sigrok's UART decoder reads on `pin`, `bit` clocks a bit (8N1 unless opts say
    otherwise, e.g. parity="even"), or its warnings (framing and parity errors)."""
    dec = ":".join(["uart:baudrate=1000000", *(f"{k}={v}" for k, v in opts.items())])
    rows = decode(bench, {"rx": pin}, dec, "uart=rx-warnings" if warnings else "uart=rx-data",
                  clock_hz=bit * 1e6)
    return texts(rows) if warnings else [int(t, 16) for t in texts(rows)]


def uart_stream(bench, pin: int, bit: float, **opts) -> list[str]:
    """sigrok's UART decoder on `pin`, the characters ("3B") and its warnings ("Parity error",
    "Frame error") in time order."""
    dec = ":".join(["uart:baudrate=1000000", *(f"{k}={v}" for k, v in opts.items())])
    return texts(decode(bench, {"rx": pin}, dec, "uart=rx-data:rx-warnings:rx-parity-err", bit * 1e6))


def spi(bench, mosi: int, sck: int, cs: int, miso: int, mode: int = 0, bits: int = 8) -> tuple:
    """(MOSI words, MISO words) as sigrok's SPI decoder reads them, CS_N active low."""
    pins = {"clk": sck, "mosi": mosi, "miso": miso, "cs": cs}
    dec = f"spi:cpol={mode >> 1}:cpha={mode & 1}:wordsize={bits}"
    return tuple([int(t, 16) for t in texts(decode(bench, pins, dec, f"spi={d}-data", 50e6))]
                 for d in ("mosi", "miso"))


def i2c(bench, scl: int, sda: int) -> list[str]:
    """The I2C conditions and bytes sigrok's decoder reads, in order: "Start", "Start repeat",
    "Address write: 50", "Data read: A5", "ACK", "NACK", "Stop"."""
    classes = "start:repeat-start:stop:ack:nack:address-read:address-write:data-read:data-write"
    rows = texts(decode(bench, {"scl": scl, "sda": sda}, "i2c", f"i2c={classes}", 50e6))
    return [t for t in rows if t not in ("Write", "Read")]     # the R/W bit, also in the address


def i2s(bench, sck: int, ws: int, sd: int) -> list[tuple[int, int]]:
    """The (left, right) samples sigrok's I2S decoder reads on `sd` (Philips I2S: the MSB one
    bit clock after the word select changes)."""
    rows = texts(decode(bench, {"sck": sck, "ws": ws, "sd": sd}, "i2s", "i2s=left:right", 50e6))
    values = [int(t.rsplit(" ", 1)[1], 16) for t in rows]
    if rows and not rows[0].startswith("Left"):
        values = values[1:]
    return list(zip(values[0::2], values[1::2]))


def can(bench, pin: int, bit: float) -> tuple[list[dict], list[str]]:
    """The CAN frames sigrok's decoder reads on the bus `pin`, `bit` clocks a bit, as
    {"id", "data", "crc", "ack"} (it shows the CRC-15 sequence but does not check it), and its
    warnings (stuffing and form errors)."""
    rows = texts(decode(bench, {"can_rx": pin}, "can:nominal_bitrate=1000000",
                        "can=id:data:crc-sequence:ack-slot:eof:warnings", bit * 1e6))
    frames, warnings = [], []
    for t in rows:
        if t.startswith("Identifier: "):
            frames.append({"id": int(t.split()[1]), "data": [], "crc": None, "ack": None})
        elif t.startswith("Data byte ") and frames:
            frames[-1]["data"].append(int(t.rsplit(" ", 1)[1], 16))
        elif t.startswith("CRC-15 sequence: ") and frames:
            frames[-1]["crc"] = int(t.rsplit(" ", 1)[1], 16)
        elif t.startswith("ACK slot: ") and frames:
            frames[-1]["ack"] = t.endswith(": ACK")
        elif t != "End of frame":
            warnings.append(t)
    return frames, warnings


def usb(bench, dp: int, dm: int, bit: float) -> list[dict]:
    """The low-speed USB packets sigrok's decoders read on D+ / D-, `bit` clocks a bit, as
    {"sync", "pid", "data", "crc_ok"}: sigrok checks the CRC16 itself."""
    rows = texts(decode(bench, {"dp": dp, "dm": dm}, "usb_signalling:signalling=low-speed,usb_packet",
                        "usb_packet=sync-ok:sync-err:pid:data:crc16-ok:crc16-err", bit * 1.5e6))
    packets: list[dict] = []
    for t in rows:
        if t.startswith("SYNC"):
            packets.append({"sync": "ERR" not in t, "pid": None, "data": [], "crc_ok": None})
        elif t.startswith("PID: ") and packets:
            packets[-1]["pid"] = t[5:]
        elif t.startswith("Databyte: ") and packets:
            packets[-1]["data"].append(int(t[10:], 16))
        elif t.startswith("CRC16") and packets:
            packets[-1]["crc_ok"] = "ERROR" not in t
    return packets


def usb_pd(bench, cc: int, bit: float) -> tuple[list[tuple[int, list[int], int]], list[str]]:
    """The USB Power Delivery messages sigrok's decoder reads on `cc`, `bit` clocks a bit, as
    (header, data objects, CRC), and its warnings (a CRC-32 that does not match, a missing EOP,
    a bad symbol): sigrok checks the CRC itself."""
    rows = texts(decode(bench, {"cc1": cc}, "usb_power_delivery",
                        "usb_power_delivery=header:data:crc:warnings", bit * 300e3, tail_edge=cc))
    messages, warnings = [], []
    for t in rows:
        if t.startswith("H:"):
            messages.append((int(t[2:], 16), [], None))
        elif t.startswith("[") and messages:
            messages[-1][1].append(int(t.split("]")[1], 16))
        elif t.startswith("CRC:") and messages:
            messages[-1] = messages[-1][:2] + (int(t[4:], 16),)
        else:
            warnings.append(t)
    return messages, warnings


def onewire(bench, dq: int, clock_hz: float) -> tuple[list[str], list[str]]:
    """What sigrok's 1-Wire decoders read on `dq`: the network layer ("Reset/presence: true",
    "ROM command: 0x33 'Read ROM'", "ROM: 0x...", "Data: 0x44") and the link layer's warnings
    (slots and pulses too short or too long, measured in microseconds: clock_hz is needed)."""
    network = texts(decode(bench, {"owr": dq}, "onewire_link,onewire_network", "onewire_network",
                           clock_hz))
    link = texts(decode(bench, {"owr": dq}, "onewire_link", "onewire_link=reset:presence:warnings",
                        clock_hz))
    return network, [t for t in link if t not in ("Reset", "Presence: true", "Presence: false")]


def jtag(bench, tdi: int, tms: int, tck: int, tdo: int) -> list[tuple[str, int, int, int]]:
    """The IR and DR scans sigrok's JTAG decoder reads, following the TAP state machine from TMS
    by itself: (register "IR" or "DR", bits shifted in on TDI, bits out on TDO, number of bits)."""
    pins = {"tdi": tdi, "tdo": tdo, "tck": tck, "tms": tms}
    scans: list[tuple[str, int, int, int]] = []
    for t in texts(decode(bench, pins, "jtag", "jtag=bitstring-tdi:bitstring-tdo", 50e6)):
        reg, line = t.split()[:2]                          # "IR TDI: 1010 (0xa), 4 bits"
        value, n = int(t.split("(")[1].split(")")[0], 16), int(t.split(", ")[-1].split()[0])
        if line == "TDI:":
            scans.append((reg, value, None, n))
        else:
            scans[-1] = (reg, scans[-1][1], value, n)
    return scans


def swd(bench, swclk: int, swdio: int) -> list[str]:
    """What sigrok's SWD decoder reads, a row each: the request ("IDCODE", "W AP4", "R
    CTRL/STAT"; DP registers by their ADIv5 names), the ACK ("OK"), the data ("0x0bc11477"),
    and any parity error it finds."""
    return texts(decode(bench, {"swclk": swclk, "swdio": swdio}, "swd", "swd", 50e6))


def ir_nec(bench, pin: int, clock_hz: float) -> list[str]:
    """What sigrok's NEC decoder reads on the IR receiver's output `pin` (active low): "Address:
    0x29", "Address#: 0xD6", "Command: 0xF8", "Command#: 0x07", "Repeat code", and its warnings.
    Times are measured: clock_hz is needed."""
    return texts(decode(bench, {"ir": pin}, "ir_nec",
                        "ir_nec=addr:addr-inv:cmd:cmd-inv:repeat-code:warnings", clock_hz))


def ws2812(bench, din: int, clock_hz: float) -> list[list[tuple[int, int, int]]]:
    """The frames sigrok's WS281x decoder reads on `din`: the (G, R, B) colour of each LED, a
    frame ending at each RESET (the low gap that latches it). Times are measured: clock_hz is
    needed."""
    frames: list[list[tuple[int, int, int]]] = [[]]
    for t in texts(decode(bench, {"din": din}, "rgb_led_ws281x", "rgb_led_ws281x=rgb:reset", clock_hz)):
        if t == "RESET":
            frames.append([])
        else:
            r, g, b = (int(t[k:k + 2], 16) for k in (1, 3, 5))
            frames[-1].append((g, r, b))
    return [f for f in frames if f]


def dali(bench, rx: int, te: float) -> list[tuple[str, int]]:
    """The DALI frames sigrok's decoder reads on `rx`, the bus through an inverting transceiver,
    `te` clocks a half-bit: ("forward", 16-bit address and command) and ("backward", the gear's
    8-bit answer). The bus is at rest before the recording (lead)."""
    rows = texts(decode(bench, {"dali": rx}, "dali:polarity=active-high", "dali=raw",
                        2400 * te, lead=round(8 * te)))
    frames: list[tuple[str, int]] = []
    for t in rows:
        if t.startswith("Raw data: "):
            if frames and frames[-1][0] == "half":
                frames[-1] = ("forward", frames[-1][1] << 8 | int(t[10:], 16))
            else:
                frames.append(("half", int(t[10:], 16)))
        elif t.startswith("Reply: "):
            frames.append(("backward", int(t[7:], 16)))
    return frames


def lin(bench, bus: int, bit: float) -> list[str]:
    """What sigrok's LIN decoder (on its UART decoder) reads on `bus`, `bit` clocks a bit:
    "Break condition", "Sync", "ID: 10 Parity: 1 (ok)", "Data: 0x11", "Checksum: 0x49", and any
    error it finds (a wrong parity or checksum)."""
    return texts(decode(bench, {"rx": bus}, "uart:baudrate=1000000,lin", "lin", bit * 1e6))


def modbus(bench, request: int, reply: int, bit: float) -> list[str]:
    """What sigrok's Modbus RTU decoder (on its UART decoder, 8N2) reads: the requests on pin
    `request` (client to server) and the replies on pin `reply`, in time order: "Slave ID: 17",
    "Function 3: Read Holding Registers", ..., and "CRC correct" or "CRC incorrect" for each
    frame: sigrok checks the CRC-16 itself."""
    return texts(decode(bench, {"rx": reply, "tx": request},
                        "uart:baudrate=1000000:stop_bits=2.0,modbus", "modbus", bit * 1e6))
