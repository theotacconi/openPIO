#!/usr/bin/env python3
"""sim/learn/learn.py against the reference devices: each recording is made by a reference UART
transmitter whose format the learner is not told; it must find that format, then its generated
programs must receive a new transmission and send one the reference receiver accepts. Frames
recorded from reference USB, CAN, MFM and biphase mark transmitters, a DALI bus with the chip's
master and a reference gear, and the chip's HDLC and 10BASE-T programs: the learner must find
their line code, stuffing, sync bits and CRC, then its generated program must receive new frames
with the CRC's verdict. Clocked lines recorded from the reference SPI master in its four modes, a
reference PS/2 keyboard and the chip's SD card commands: the clock, its mode, the select pin or
the pauses, the CRC; the generated program receives new frames. Pulse-width codes recorded from the
reference NEC remote, Sony and 433 MHz sensor frames and the chip's WS2812 program: the pulse
that carries the bits, its widths, the leader, the CRC; the generated program receives new frames.

    python3 test/learn/test_learn.py
"""
import random
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "sim" / "learn"), str(ROOT / "sim" / "model"), str(ROOT / "test" / "shared")]
from bench import Bench, Device, Script                  # noqa: E402
from chip import IRQ                                     # noqa: E402
from asm import assemble                                 # noqa: E402
from devices import (BmcTx, CanNode, DaliBus, DaliGear, NecRemote, Ps2Device,  # noqa: E402
                     SpiMaster, UartRx, UartTx, UsbTx, WiredAndBus, can_bits, can_crc15, crc7_sd,
                     crc16_usb, crc16_x25, mfm_halves, mfm_sector, nec_levels, ps2_bits)
from learn import (Capture, bursts, clocked_rx_program, clocked_timing, decode,  # noqa: E402
                   write_sigrok, rx_frames as frames_received,
                   frame_timing, frames_rx_program, infer_async, infer_clocked, infer_frames,
                   infer_pulses,
                   pau_directives, pulses_check, pulses_rx_program, pulses_timing, rx_program,
                   timing, tx_program, word_value)

PIN = 3
FORMATS = [  # (data bits, parity, stop bits, idle level, clocks a bit)
    (8, None, 1, 1, 40), (8, None, 1, 0, 37.3), (7, "even", 1, 1, 52), (8, "odd", 1, 0, 100),
    (8, None, 2, 1, 26.5), (7, None, 2, 0, 61), (9, None, 1, 1, 33), (5, "even", 2, 1, 44),
]


class Inverted(Device):
    """A device behind an inverter on `pin`: it sees and drives that pin inverted."""

    def __init__(self, dev: Device, pin: int) -> None:
        super().__init__()
        self.dev, self.pin = dev, pin
        self.drive = {p: v ^ (p == pin) for p, v in dev.drive.items()}

    def tick(self, t: int, levels: int) -> None:
        self.dev.tick(t, levels ^ 1 << self.pin)
        self.drive = {p: v ^ (p == self.pin) for p, v in self.dev.drive.items()}
        self.errors = self.dev.errors


def transmitter(fmt, data, seed, start=200):
    n, parity, stop, idle, bit = fmt
    rnd = random.Random(seed)
    gaps = [0 if rnd.random() < .5 else rnd.randrange(1, 4) for _ in data]   # some back to back
    tx = UartTx(PIN, bit, data, start=start, gap=gaps, data_bits=n, parity=parity, stop_bits=stop)
    return tx if idle else Inverted(tx, PIN)


def values(fmt, seed, count=40):
    rnd = random.Random(seed)
    return [rnd.randrange(1 << fmt[0]) for _ in range(count)]


def record(fmt, data, seed):
    bench = Bench(pullups=fmt[3] << PIN)
    tx = bench.add(transmitter(fmt, data, seed))
    bench.run((tx.dev if isinstance(tx, Inverted) else tx).end + 50)
    return Capture.from_levels(bench.trace, PIN)


class Learn(unittest.TestCase):

    def check(self, p, fmt):
        n, parity, stop, idle, bit = fmt
        self.assertEqual((p.data_bits, p.parity, p.stop_bits, p.idle), (n, parity, stop, idle))
        self.assertAlmostEqual(p.bit, bit, delta=bit * 0.01)

    def test_infers_the_format(self):
        for k, fmt in enumerate(FORMATS):
            with self.subTest(fmt=fmt):
                data = values(fmt, k)
                cap = record(fmt, data, k)
                p = infer_async(cap)
                self.check(p, fmt)
                self.assertEqual(decode(cap, p), data)

    def test_from_the_chip_recording(self):
        """The recording made by programs/logic/edges.asm on the chip, through the host."""
        fmt, data = (8, "even", 1, 1, 50), values((8,), 9)
        bench = Bench(pullups=1 << PIN)
        bench.load(0, (ROOT / "programs/logic/edges.asm").read_text(), in_base=PIN)
        tx = bench.add(transmitter(fmt, data, 9))
        bench.run(4)                                     # the inputs through the synchronizer,
        bench.start(1)                                   # as a host takes far longer to load
        bench.run(tx.end + 200)
        p = infer_async(Capture.from_edges_program(bench.received[0]))
        self.check(p, fmt)

    def test_from_the_chip_recording_of_all_pins(self):
        """The same from programs/logic/record.asm: every pin recorded, the UART line taken."""
        fmt, data = (7, "odd", 2, 0, 45), values((7,), 11)
        bench = Bench(pullups=0)
        bench.load(0, (ROOT / "programs/logic/record.asm").read_text())
        tx = bench.add(transmitter(fmt, data, 11))
        bench.run(4)
        bench.start(1)
        bench.run(tx.dev.end + 200)
        p = infer_async(Capture.from_record_program(bench.received[0], PIN))
        self.check(p, fmt)

    def test_generated_programs(self):
        """Learned from one transmission: the RX program receives another, the TX program
        sends values the reference receiver checks (format, parity, stop bits)."""
        for k, fmt in enumerate(FORMATS[:6]):
            with self.subTest(fmt=fmt):
                p = infer_async(record(fmt, values(fmt, k), k))
                h, div, frac = timing(p)
                data = values(fmt, 100 + k)
                bench = Bench(pullups=fmt[3] << PIN)
                bench.load(0, rx_program(p, PIN), clkdiv_int=div, clkdiv_frac=frac)
                tx = bench.add(transmitter(fmt, data, 100 + k, start=50))
                bench.start(1)
                bench.run((tx.dev if isinstance(tx, Inverted) else tx).end + 100)
                self.assertEqual([word_value(p, w) for w in bench.received[0]], data)
                self.assertFalse(any(bench.chip.sms[0].irq_flags[:2]), "no framing or parity error")

                if fmt[4] != int(fmt[4]):
                    continue                             # the reference receiver counts whole clocks
                bench = Bench(pullups=fmt[3] << PIN)
                bench.load(0, tx_program(p, PIN), clkdiv_int=div, clkdiv_frac=frac)
                rx = UartRx(PIN, fmt[4], data_bits=fmt[0], parity=fmt[1], stop_bits=fmt[2])
                bench.add(rx if fmt[3] else Inverted(rx, PIN))
                bench.send(0, data)
                bench.start(1)
                bench.run(round(len(data) * p.length * fmt[4]) + 500)
                self.assertEqual((rx.frames, rx.errors), (data, []))


def bits(data: list[int], lsb_first: bool = True) -> list[int]:
    return [b >> (i if lsb_first else 7 - i) & 1 for b in data for i in range(8)]


def usb_bench(packets) -> Bench:
    bench = Bench()
    bench.add(UsbTx(PIN, PIN + 1, 40, packets, start=100, gap=16))
    return bench


def can_bench(frames) -> Bench:
    """A node sending `frames` after 11 idle bits, another acknowledging them."""
    nodes = [CanNode(50, send=frames, idle=0), CanNode(50)]
    bench = Bench(pullups=1 << PIN)
    bench.add(WiredAndBus([], [PIN], nodes))
    return bench


def mfm_levels(sectors: list[list[int]]) -> list[int]:
    """The line of sectors (each the bits of mfm_sector), 800 clocks of silence after each."""
    levels, level = [0] * 400, 0
    for sector in sectors:
        hs = mfm_halves(sector, level)
        levels += [h << PIN for h in hs for _ in range(20)] + [hs[-1] << PIN] * 800
        level = hs[-1]
    return levels


def hdlc_trace(frames: list[list[int]]) -> list[int]:
    """Pin 0 of programs/hdlc/hdlc_tx.asm sending the frames, the line idle before each."""
    bench = Bench()
    bench.load(0, (ROOT / "programs/hdlc/hdlc_tx.asm").read_text())
    bench.start(1)
    bench.run(300)                                       # the program sets the line idle
    start = len(bench.trace)
    for f in frames:
        bench.send(0, [len(f)] + f)
        bench.run((len(f) + 4) * 8 * 8 * 6 // 5 + 400)
    return [lv & 1 for lv in bench.trace[start:]]


def hdlc_bits(data: list[int]) -> list[int]:
    """A frame's bits from the opening flag to the FCS, destuffed."""
    fcs = crc16_x25(data)
    return bits([0x7E, *data, fcs & 0xFF, fcs >> 8])


def dali_wave(frames: list[int], answers: dict[int, int]) -> list[int]:
    """The DALI bus as programs/dali/dali_master.asm sends the forward frames (24 clocks a
    half-bit) and a gear answers the queries in `answers` 5.5 ms after."""
    te = 24
    bench = Bench()
    bench.load(0, (ROOT / "programs/dali/dali_master.asm").read_text(), clkdiv_int=3)
    bus = bench.add(DaliBus(0, 1, [DaliGear(te, answers, settle=round(5.5 * 2.4 * te))]))
    bench.send(0, frames)
    bench.start(1)
    bench.run(len(frames) * 100 * te, until=lambda: len(bench.received[0]) == len(frames))
    bench.run(500)
    assert bench.errors() == []
    return bus.wave


def dali_frames(frames: list[int], answers: dict[int, int]) -> list[list[int]]:
    """The bits on the bus: each forward frame, start bit and 16 bits MSB first, then its
    answer, start bit and 8 bits."""
    out = []
    for f in frames:
        out.append([1] + [f >> 15 - i & 1 for i in range(16)])
        if f in answers:
            out.append([1] + [answers[f] >> 7 - i & 1 for i in range(8)])
    return out


class LearnFrames(unittest.TestCase):
    """Self-clocked frames: a burst of edges each, idle between."""

    def learn(self, levels, pin=PIN):
        return infer_frames(Capture.from_levels(levels, pin))

    def receive(self, p, bench: Bench, clocks: int) -> list[tuple[list[int], int]]:
        """The frames the generated program receives from the bench's devices."""
        _, div, frac = frame_timing(p)
        bench.load(0, frames_rx_program(p, PIN), clkdiv_int=div, clkdiv_frac=frac)
        bench.run(4)                                     # the line through the synchronizer
        bench.start(1)
        bench.run(clocks)
        return frames_received(bench.received[0])

    def check_received(self, got, sent, verdicts):
        """Each frame starts with its sent bits; the end words carry the verdicts."""
        self.assertEqual([v for _, v in got], verdicts)
        for (b, _), want in zip(got, sent):
            self.assertEqual(b[:len(want)], want)

    def check(self, p, code, stuff, crc, sync=None):
        self.assertEqual((p.code, p.stuff), (code, stuff))
        self.assertEqual(p.crc and (p.crc.width, p.crc.poly, p.crc.init, p.crc.xorout), crc)
        if sync:
            self.assertEqual(p.sync[:len(sync)], sync)

    def test_usb(self):
        """Low speed on D-: NRZI, a 0 stuffed after six 1s (the 0xFF bytes), CRC-16/USB of the
        data packets."""
        rnd = random.Random(1)
        packets = [(0xC3, [0xFF, 0xFF] + [rnd.randrange(256) for _ in range(n)])
                   for n in (2, 4, 2, 6, 4, 1)]
        bench = usb_bench(packets)
        bench.run(bench.devices[0].end + 100)
        p = self.learn(bench.trace)
        self.check(p, "nrzi", (0, 6), (16, 0x8005, 1, 1), [0] * 7 + [1] + bits([0xC3]))
        self.assertEqual(p.crc.start, 16, "after SYNC and PID")
        self.assertAlmostEqual(p.half, 20, delta=0.2)

        packets = [(0x4B, [rnd.randrange(256) for _ in range(n)]) for n in (3, 8, 0, 5)]
        packets[2] += (0x1234,)                         # a wrong CRC
        bench = usb_bench(packets)
        got = self.receive(p, bench, bench.devices[0].end + 200)
        sent = [bits([0x80, pid, *d, *(c[0] if c else crc16_usb(d)).to_bytes(2, "little")])
                for pid, d, *c in packets]
        self.check_received(got, sent, [3, 3, 1, 3])

    def test_can(self):
        """CAN 2.0A on a wired-AND bus, a second node acknowledging: NRZ, the complement after
        five equal bits, CRC-15 from the start of frame."""
        rnd = random.Random(2)
        frames = [(rnd.randrange(2048), [rnd.randrange(256) for _ in range(n)])
                  for n in (1, 3, 3, 8, 0, 5)]
        bench = can_bench(frames)
        bus = bench.devices[0]
        bench.run(sum(160 + 8 * len(d) for _, d in frames) * 50,
                  until=lambda: not bus.nodes[0].send)
        bench.run(2000)
        self.assertEqual(bus.errors, [])
        p = self.learn(bench.trace)
        self.check(p, "nrz", (1, 5), (15, 0x4599, 0, 0), [0])

        frames = [(rnd.randrange(2048), [rnd.randrange(256) for _ in range(n)])
                  for n in (2, 7, 0, 4)]
        got = self.receive(p, can_bench(frames), sum(170 + 8 * len(d) for _, d in frames) * 50)
        sent = [b + [can_crc15(b) >> i & 1 for i in range(14, -1, -1)]
                for b in (can_bits(*f) for f in frames)]
        self.check_received(got, sent, [3] * 4)

    def test_mfm(self):
        """Floppy sectors: MFM (the PAU's Miller code), CRC-16/CCITT after the address mark."""
        rnd = random.Random(3)
        p = self.learn(mfm_levels([mfm_sector([rnd.randrange(256) for _ in range(n)])
                                   for n in (8, 12, 8, 16, 5)]))
        self.check(p, "miller", None, (16, 0x1021, 1, 0), bits([0, 0, 0, 0, 0xA1], lsb_first=False))

        sectors = [mfm_sector([rnd.randrange(256) for _ in range(n)]) for n in (10, 6, 14)]
        sectors[1][50] ^= 1                             # a bit flipped on the disk
        levels = mfm_levels(sectors)
        bench = Bench()
        bench.add(Script([(t, {PIN: lv >> PIN & 1}) for t, lv in enumerate(levels)
                          if t == 0 or lv != levels[t - 1]]))
        got = self.receive(p, bench, len(levels))
        self.check_received(got, sectors, [3, 1, 3])

    def test_biphase_mark(self):
        """Frames of random bits without a CRC: biphase mark, told from its shifted codes by
        the first edge of every frame on a bit boundary."""
        rnd = random.Random(4)
        frames = [[rnd.randrange(2) for _ in range(rnd.randrange(40, 120))] for _ in range(5)]
        bench = Bench()
        tx = bench.add(BmcTx(PIN, 40, frames, start=100, gap=20))
        bench.run(tx.end + 100)
        p = self.learn(bench.trace)
        self.check(p, "bmc", None, None)
        self.assertEqual(p.frames, frames)

        frames = [[rnd.randrange(2) for _ in range(rnd.randrange(40, 120))] for _ in range(3)]
        tx = BmcTx(PIN, 40.4, frames, start=100, gap=20)  # a transmitter 1 % slow
        bench = Bench()
        bench.add(tx)
        got = self.receive(p, bench, tx.end + 100)
        self.check_received(got, frames, [1] * 3)       # no CRC: no verdict

    def test_ethernet_from_the_chip(self):
        """10BASE-T frames sent by programs/ethernet/eth_tx.asm on the chip: Manchester, the
        preamble and SFD, the FCS (CRC-32)."""
        rnd = random.Random(5)
        bench = Bench()
        bench.load(0, (ROOT / "programs/ethernet/eth_tx.asm").read_text())
        bench.start(1)
        for n in (20, 26, 20, 32):                       # bytes after the SFD, the FCS's 4 too
            f = [rnd.randrange(256) for _ in range(n - 4)]
            bench.send(0, [len(f) // 2] + [f[i] | f[i + 1] << 8 for i in range(0, len(f), 2)])
            bench.run(200 * n, until=lambda: not bench.to_send[0])
            bench.write(0, IRQ, 1 << 8)                  # flag 0: a frame is loaded
            bench.run((n + 30) * 32)
        p = self.learn(bench.trace, 0)
        self.check(p, "manchester", None, (32, 0x04C11DB7, 1, 1), bits([0x55] * 7 + [0xD5]))
        self.assertEqual(p.crc.start, 64)
        self.assertIn(".crc width=32 poly=0x4C11DB7 init=ones xorout=1", pau_directives(p)[0])
        with self.assertRaisesRegex(ValueError, "too fast"):
            frames_rx_program(p, 0)                      # 2 clocks a half-bit

    def test_hdlc_from_the_chip(self):
        """programs/hdlc/hdlc_tx.asm, the line idle between frames: NRZI, a 0 after five 1s,
        the flag 0x7E, the FCS (CRC-16/X.25) after it. A bit flipped on the line makes the CRC
        wrong."""
        rnd = random.Random(12)
        frames = [[rnd.randrange(256) for _ in range(n)] for n in (4, 9, 2, 12, 6, 3)]
        p = self.learn(hdlc_trace(frames), 0)
        self.check(p, "nrzi", (0, 5), (16, 0x1021, 1, 1), bits([0x7E]))
        self.assertEqual(p.crc.start, 8, "after the flag")
        self.assertEqual([f[:-8] for f in p.frames], [hdlc_bits(f) for f in frames])

        frames = [[rnd.randrange(256) for _ in range(n)] for n in (5, 8, 3)]
        levels = hdlc_trace(frames)
        edges = bursts(Capture.from_levels(levels), 80)[1][1]
        t = next(t for t in range(edges[0] + 8 * 40, edges[-1], 8) if t not in edges)
        levels[t:] = [lv ^ 1 for lv in levels[t:]]      # NRZI: a 1 in the second frame now a 0
        bench = Bench()
        bench.add(played([lv << PIN for lv in levels], [PIN]))
        got = self.receive(p, bench, len(levels))
        self.assertEqual([v for _, v in got], [3, 1, 3])
        for (b, _), f in zip(got[::2], frames[::2]):
            self.assertEqual(b[:len(hdlc_bits(f))], hdlc_bits(f))

    def test_hdlc_one_frame_corrupted(self):
        """A bit flipped in one recorded frame: the CRC is learned from the others, and that
        frame is received with the CRC wrong."""
        rnd = random.Random(17)
        frames = [[rnd.randrange(256) for _ in range(n)] for n in (4, 9, 2, 12, 6, 3, 7, 5)]
        levels = hdlc_trace(frames)
        edges = bursts(Capture.from_levels(levels), 80)[3][1]
        t = next(t for t in range(edges[0] + 8 * 50, edges[-1], 8) if t not in edges)
        levels[t:] = [lv ^ 1 for lv in levels[t:]]      # NRZI: a 1 in the fourth frame now a 0
        p = self.learn(levels, 0)
        self.check(p, "nrzi", (0, 5), (16, 0x1021, 1, 1), bits([0x7E]))
        self.assertEqual((p.crc.start, p.crc.spare), (8, 1))
        bench = Bench()
        bench.add(played([lv << PIN for lv in levels], [PIN]))
        got = self.receive(p, bench, len(levels))
        self.assertEqual([v for _, v in got], [3, 3, 3, 1, 3, 3, 3, 3])

    def test_dali(self):
        """A DALI bus: forward frames from programs/dali/dali_master.asm, a gear's backward
        frames, the next forward frame 6 half-bits after an answer: Manchester, idle high, a
        start bit 1, frames of 17 and 9 bits, no CRC."""
        answers = {0x0190: 0xA5, 0x03A0: 0x5A, 0xFF90: 0x04}
        frames = [0x0105, 0x0190, 0x03A0, 0x05A1, 0xFF90, 0x1234]
        p = self.learn(dali_wave(frames, answers), 0)
        self.check(p, "manchester", None, None, [1])
        self.assertEqual((p.inv, round(p.half)), (0, 24))
        self.assertEqual(p.frames, dali_frames(frames, answers))

        frames = [0x0190, 0x2B05, 0xFF90, 0x03A0]
        wave = dali_wave(frames, answers)
        bench = Bench()
        bench.add(played([lv << PIN for lv in wave], [PIN]))
        got = self.receive(p, bench, len(wave))
        self.assertEqual(got, [(f, 1) for f in dali_frames(frames, answers)])


def played(levels: list[int], pins) -> Script:
    """A device driving `pins` as in a recording, a level per clock."""
    return Script([(t, {k: lv >> k & 1 for k in pins}) for t, lv in enumerate(levels)
                   if t == 0 or lv != levels[t - 1]])


MOSI, SCK, CS, MISO = 0, 1, 2, 3
SD_COMMANDS = [(0, 0), (8, 0x1AA), (55, 0), (41, 0x40000000), (17, 0x12345), (24, 0x200),
               (16, 512), (13, 0x10000), (58, 0)]


def sd_command(index: int, arg: int) -> list[int]:
    head = [0x40 | index, *(arg >> s & 0xFF for s in (24, 16, 8, 0))]
    return head + [crc7_sd(head) << 1 | 1]


class LearnClocked(unittest.TestCase):
    """A clock pin, data pins, maybe a select pin."""

    def ps2(self, keys, bad_parity=frozenset()):
        """A PS/2 keyboard sending the keys: DATA on MOSI, CLK on SCK."""
        bench = Bench(pullups=1 << MOSI | 1 << SCK)
        bench.add(Ps2Device(SCK, MOSI, keys, bad_parity=bad_parity))
        return bench, 50 + 1080 * len(keys) + 300

    def learn(self, levels, pins):
        return infer_clocked({k: Capture.from_levels(levels, k) for k in pins})

    def spi(self, mode, transfers):
        bench = Bench(pullups=1 << MISO | 1 << CS)
        bench.add(SpiMaster(SCK, MOSI, MISO, CS, transfers, half=14, start=50, lead=14, gap=40,
                            mode=mode))
        return bench

    def receive(self, p, bench, clocks, select=None):
        bench.load(0, clocked_rx_program(p, MOSI, select), clkdiv_int=clocked_timing(p)[0])
        bench.start(1)
        bench.run(clocks)
        return frames_received(bench.received[0])

    def test_spi_every_mode(self):
        """MOSI on SCK while CS_N is low, in each mode; no CRC."""
        rnd = random.Random(7)
        for mode in range(4):
            with self.subTest(mode=mode):
                transfers = [[rnd.randrange(256) for _ in range(n)] for n in (2, 5, 1, 3)]
                bench = self.spi(mode, transfers)
                bench.run(bench.devices[0].end + 50)
                (p,) = self.learn(bench.trace, range(4))          # MISO idle: no data
                self.assertEqual((p.clock, p.data, p.select), (SCK, MOSI, (CS, 0)))
                self.assertEqual((p.cpol, p.cpha, p.crc), (mode >> 1, mode & 1, None))
                self.assertEqual(p.frames, [bits(t, lsb_first=False) for t in transfers])

                transfers = [[rnd.randrange(256) for _ in range(n)] for n in (3, 1, 4)]
                bench = self.spi(mode, transfers)
                got = self.receive(p, bench, bench.devices[0].end + 100, CS)
                self.assertEqual(got, [(bits(t, lsb_first=False), 1) for t in transfers])

    def test_ps2_keyboard(self):
        """A PS/2 keyboard's bytes: data set while the clock is high, sampled as it falls (mode
        2), frames 3 clock periods apart, a start bit 0, odd parity: a 1-bit CRC, believed
        from 41 bytes. A wrong parity bit makes the CRC wrong."""
        rnd = random.Random(13)
        keys = [rnd.randrange(256) for _ in range(48)]
        bench, end = self.ps2(keys)
        bench.run(end)
        (p,) = self.learn(bench.trace, (MOSI, SCK))
        self.assertEqual((p.clock, p.data, p.select, p.cpol, p.cpha), (SCK, MOSI, None, 1, 0))
        self.assertEqual((p.sync, p.crc.width, p.crc.poly), ([0], 1, 1))   # the stop bit too
        self.assertEqual(p.frames, [ps2_bits(k) for k in keys])

        keys = [rnd.randrange(256) for _ in range(4)]
        bench, end = self.ps2(keys, bad_parity={1})
        got = self.receive(p, bench, end)
        want = [ps2_bits(k) for k in keys]
        want[1][9] ^= 1
        self.assertEqual(got, [(b, v) for b, v in zip(want, (3, 1, 3, 3))])

    def test_sd_commands_from_the_chip(self):
        """programs/sd/sd_command.asm in each mode: the start bits 01, CRC-7 0x09, the end bit;
        frames by CS_N, and without it by the clock's pauses. A bit flipped on the line makes
        the CRC wrong."""
        prog = assemble((ROOT / "programs/sd/sd_command.asm").read_text())
        for mode in range(4):
            bench = Bench(pullups=1 << CS)
            bench.load(0, prog, pau_mode=prog.cfg["pau_mode"] | (mode >> 1) << 5 | (mode & 1) << 6)
            bench.start(1)
            bench.run(200)                               # the bus at rest before the first command
            for c in SD_COMMANDS:
                bench.send(0, [b << 8 for b in sd_command(*c)[:5]])
                bench.run(8 * 6 * 8 + 40)
            levels = bench.trace
            for pins, select in ((range(3), (CS, 0)), (range(2), None)):
                with self.subTest(mode=mode, select=select):
                    (p,) = self.learn(levels, pins)
                    self.assertEqual((p.cpol, p.cpha, p.select, p.sync),
                                     (mode >> 1, mode & 1, select, [0, 1]))
                    c = p.crc
                    self.assertEqual((c.width, c.poly, c.init, c.start, c.tail), (7, 0x09, 0, 0, 1))
                    self.assertEqual(p.frames, [bits(sd_command(*c), lsb_first=False)
                                                for c in SD_COMMANDS])

                    clk = Capture.from_levels(levels, SCK)
                    sampled = [t for t in clk.edges if clk.level_at(t) == (p.cpol == p.cpha)]
                    t = sampled[48 + 20]                 # the second command's bit 20
                    bad = [lv ^ (abs(k - t) <= 3) << MOSI for k, lv in enumerate(levels)]
                    bench = Bench()
                    bench.add(played(bad[100:], pins))   # from the bus at rest (SCK at CPOL)
                    got = self.receive(p, bench, len(bad) + 100, select and CS)
                    self.assertEqual([v for _, v in got], [3, 1] + [3] * 7)
                    self.assertEqual([b for b, _ in got][2:], p.frames[2:])



def pulse_script(frames: list[list[tuple[int, float]]], scale: float, idle: int,
                 gap: float) -> tuple[Script, int]:
    """A device driving PIN through frames of (level, microseconds), `scale` clocks a
    microsecond, idle `gap` microseconds before and after each; and the clock it ends."""
    steps, t = [(0, {PIN: idle})], gap * scale
    for f in frames:
        for lv, us in f + [(idle, gap)]:
            steps.append((round(t), {PIN: lv}))
            t += us * scale
    return Script(steps), round(t)


def sirc_levels(command: int, address: int) -> list[tuple[int, float]]:
    """A Sony SIRC 12-bit frame at an IR receiver's output: the leader, then 7 command and 5
    address bits LSB first, a 1 a mark of 1200 us, a 0 of 600, 600 us spaces."""
    out = [(0, 2400), (1, 600)]
    for b in bits([command | address << 7, address >> 1])[:12]:
        out += [(0, 1200 if b else 600), (1, 600)]
    return out[:-1]


def crc8(bits_: list[int], poly: int = 0x31) -> int:
    reg = 0
    for b in bits_:
        reg = (reg << 1 & 0xFF) ^ (poly if reg >> 7 ^ b else 0)
    return reg


def wh2_bits(ident: int, temp: int, humidity: int) -> list[int]:
    """A Fine Offset WH2 weather sensor's 48 bits: 0xFF, the type 4, the sensor, the
    temperature (12 bits), the humidity, CRC-8 0x31 of the 32 bits after 0xFF."""
    body = [(4 << 28 | ident << 20 | temp << 8 | humidity) >> 31 - k & 1 for k in range(32)]
    return [1] * 8 + body + [crc8(body) >> 7 - k & 1 for k in range(8)]


def wh2_levels(bits_: list[int]) -> list[tuple[int, float]]:
    """At a 433 MHz receiver's output (high with the carrier): a 1 a mark of 500 us, a 0 of
    1500, 1000 us spaces."""
    return [p for b in bits_ for p in ((1, 500 if b else 1500), (0, 1000))][:-1]


def ws2812_trace(colours: list[list[tuple[int, int, int]]]) -> list[int]:
    """The line of programs/ws2812/ws2812.asm at 48 MHz sending frames of (G, R, B), each
    latched by a pause."""
    bench = Bench()
    bench.load(0, (ROOT / "programs/ws2812/ws2812.asm").read_text(), clkdiv_int=6)
    bench.start(1)
    for leds in colours:
        b = bits([c for led in leds for c in led], lsb_first=False)
        bench.send(0, [int("".join(map(str, b[k:k + 16])), 2) for k in range(0, len(b), 16)])
        bench.run(60 * len(b) + 3000)
    return bench.trace


class LearnPulses(unittest.TestCase):
    """Pulse-width codes: a mark and a space a bit, one of them two widths."""

    def receive(self, p, device, clocks):
        bench = Bench()
        bench.add(device)
        bench.load(0, pulses_rx_program(p, PIN), clkdiv_int=pulses_timing(p)[0])
        bench.start(1)
        bench.run(clocks)
        return frames_received(bench.received[0])

    def test_nec(self):
        """The reference NEC remote: pulse distance, the leader of 9 ms and 4.5 ms; repeat codes
        and a lone burst are frames of other kinds. Each byte and its complement XOR to 0: a
        CRC-8 x^8 + 1, partly the remote's check."""
        rnd = random.Random(8)
        frames = [(rnd.randrange(256), rnd.randrange(256)) for _ in range(6)]
        frames[2:2] = ["repeat", ("glitch", 3000), "repeat"]
        bench = Bench()
        remote = bench.add(NecRemote(PIN, frames, us=0.2, start=100, gap=40000))
        bench.run(remote.end + 100)
        p = infer_pulses(Capture.from_levels(bench.trace, PIN))
        self.assertEqual((p.carrier, p.idle, p.one, p.ignored), ("space", 1, 1, 3))
        self.assertEqual([round(w) for w in p.leader], [1800, 900])
        self.assertEqual((p.crc.width, p.crc.poly), (8, 0x01))
        self.assertEqual(p.frames, [bits([a, a ^ 0xFF, c, c ^ 0xFF])
                                    for a, c in frames[:2] + frames[5:]])

        frames = [(rnd.randrange(256), rnd.randrange(256)) for _ in range(4)]
        sent = frames[:1] + ["repeat", ("glitch", 3000)] + frames[1:]
        for us in (0.18, 0.22):                          # every time 10 % shorter, longer
            with self.subTest(us=us):
                remote = NecRemote(PIN, sent, us=us, start=100, gap=40000)
                got = self.receive(p, remote, remote.end + 100)
                self.assertEqual(got, [(bits([a, a ^ 0xFF, c, c ^ 0xFF]), 3) for a, c in frames])

        levels = nec_levels(frames[0])
        levels[2 + 2 * 3 + 1] = (1, 1687.5 + 562.5 - levels[2 + 2 * 3 + 1][1])   # bit 3 flipped
        script, end = pulse_script([levels], 0.2, 1, 40000)
        got = self.receive(p, script, end)
        self.assertEqual([v for _, v in got], [1])

    def test_nec_more_repeats_than_frames(self):
        """A remote that sends a repeat code even after a short press, two after a longer one:
        the frames are fewer than the repeat codes but hold more pulses, and are the ones
        learned."""
        rnd = random.Random(28)
        frames = [(0x00, rnd.randrange(256)) for _ in range(5)]
        sent = [x for f in frames for x in (f, "repeat", "repeat")]
        bench = Bench()
        remote = bench.add(NecRemote(PIN, sent, us=0.2, start=100, gap=40000))
        bench.run(remote.end + 100)
        p = infer_pulses(Capture.from_levels(bench.trace, PIN))
        self.assertEqual((p.carrier, p.ignored), ("space", 10))
        self.assertEqual(p.frames, [bits([a, a ^ 0xFF, c, c ^ 0xFF]) for a, c in frames])

    def test_nec_one_key_corrupted(self):
        """Ten keys, a bit of the fourth flipped (a reflection): the check is learned from the
        other nine, and a frame received with that bit flipped is still found wrong."""
        rnd = random.Random(18)
        keys = [(0x04, rnd.randrange(256)) for _ in range(10)]
        levels = [nec_levels(k) for k in keys]
        levels[3][2 + 2 * 19 + 1] = (1, 1687.5 + 562.5 - levels[3][2 + 2 * 19 + 1][1])
        script, end = pulse_script(levels, 0.2, 1, 40000)
        bench = Bench()
        bench.add(script)
        bench.run(end)
        p = infer_pulses(Capture.from_levels(bench.trace, PIN))
        self.assertEqual((p.crc.width, p.crc.poly, p.crc.spare), (8, 0x01, 1))
        self.assertIsNotNone(pulses_check(p))
        script, end = pulse_script([nec_levels(keys[0]), levels[3]], 0.2, 1, 40000)
        self.assertEqual([v for _, v in self.receive(p, script, end)], [3, 1])

    def test_sirc(self):
        """Sony SIRC at an IR receiver's output: pulse width, idle high, the leader's mark."""
        rnd = random.Random(9)
        frames = [(rnd.randrange(128), rnd.randrange(32)) for _ in range(6)]
        script, end = pulse_script([sirc_levels(*f) for f in frames], 0.2, 1, 30000)
        bench = Bench()
        bench.add(script)
        bench.run(end)
        p = infer_pulses(Capture.from_levels(bench.trace, PIN))
        self.assertEqual((p.carrier, p.idle, p.one, p.crc), ("mark", 1, 1, None))
        self.assertEqual([round(w) for w in p.leader], [480, 120])
        want = [bits([c | a << 7, a >> 1])[:12] for c, a in frames]
        self.assertEqual(p.frames, want)

        frames = [(rnd.randrange(128), rnd.randrange(32)) for _ in range(3)]
        script, end = pulse_script([sirc_levels(*f) for f in frames], 0.22, 1, 30000)
        got = self.receive(p, script, end)
        self.assertEqual(got, [(bits([c | a << 7, a >> 1])[:12], 1) for c, a in frames])

    def test_433_mhz_sensor(self):
        """Fine Offset WH2 frames: pulse width, a short mark a 1, found by the CRC-8 0x31 after
        the 0xFF preamble (the long-is-1 bits have none)."""
        rnd = random.Random(10)
        sensor = [wh2_bits(0x5A, rnd.randrange(4096), rnd.randrange(100)) for _ in range(8)]
        script, end = pulse_script([wh2_levels(b) for b in sensor], 0.1, 0, 20000)
        bench = Bench()
        bench.add(script)
        bench.run(end)
        p = infer_pulses(Capture.from_levels(bench.trace, PIN))
        self.assertEqual((p.carrier, p.idle, p.one, p.leader), ("mark", 0, 0, []))
        c = p.crc
        self.assertEqual((c.width, c.poly, c.init, c.start, c.tail), (8, 0x31, 0, 8, 0))
        self.assertEqual(p.frames, sensor)

        sensor = [wh2_bits(0x5A, rnd.randrange(4096), rnd.randrange(100)) for _ in range(3)]
        sensor[1][20] ^= 1                               # a bit flipped on the air
        script, end = pulse_script([wh2_levels(b) for b in sensor], 0.1, 0, 20000)
        got = self.receive(p, script, end)
        self.assertEqual(got, [(b, v) for b, v in zip(sensor, (3, 1, 3))])

    def test_ws2812_from_the_chip(self):
        """programs/ws2812/ws2812.asm: pulse width, idle low, a long mark with a short space;
        frames latched by the pause."""
        rnd = random.Random(11)
        colours = [[tuple(rnd.randrange(256) for _ in range(3)) for _ in range(n)]
                   for n in (2, 4, 2, 6)]
        p = infer_pulses(Capture.from_levels(ws2812_trace(colours), 0))
        self.assertEqual((p.carrier, p.idle, p.one, p.leader, p.crc), ("mark", 0, 1, [], None))
        self.assertEqual((round(p.short), round(p.long)), (18, 42))
        want = [bits([c for led in leds for c in led], lsb_first=False) for leds in colours]
        self.assertEqual(p.frames, want)

        colours = [[tuple(rnd.randrange(256) for _ in range(3)) for _ in range(n)]
                   for n in (4, 2)]
        levels = [0] * 3000 + ws2812_trace(colours)      # a pause before the first
        got = self.receive(p, played([lv << PIN for lv in levels], [PIN]), len(levels))
        self.assertEqual(got, [(bits([c for led in leds for c in led], lsb_first=False), 1)
                               for leds in colours])


class LearnRecordings(unittest.TestCase):
    """Recordings as a logic analyzer saves them (sigrok session files): sampled at another rate
    than the chip's clock, started and stopped in the middle of a frame."""

    def recorded(self, levels: list[int], cut: tuple[int, int], rate: float, clock_hz: float):
        """The channels of levels[cut[0]:cut[1]] saved at `rate` samples a second, read back
        in clocks of `clock_hz`."""
        with tempfile.TemporaryDirectory() as d:
            write_sigrok(Path(d) / "rec.sr", levels[cut[0]:cut[1]], rate)
            return Capture.from_sigrok(Path(d) / "rec.sr", clock_hz)

    def test_usb_cut_at_both_ends(self):
        """USB packets, the first and the last cut by the recording: they are left out; the
        clock twice the sample rate doubles every time."""
        rnd = random.Random(14)
        packets = [(0xC3, [0xFF, 0xFF] + [rnd.randrange(256) for _ in range(n)])
                   for n in (3, 2, 4, 2, 6, 4, 1, 5)]
        bench = usb_bench(packets)
        bench.run(bench.devices[0].end + 100)
        bs = bursts(Capture.from_levels(bench.trace, PIN), 400)
        cut = ((bs[0][1][0] + bs[0][1][-1]) // 2, (bs[-1][1][0] + bs[-1][1][-1]) // 2)
        cap = self.recorded(bench.trace, cut, 24e6, 48e6)[f"D{PIN}"]
        p = infer_frames(cap)
        self.assertEqual((p.code, p.stuff, p.crc.width, p.crc.poly), ("nrzi", (0, 6), 16, 0x8005))
        self.assertAlmostEqual(p.half, 40, delta=0.4)
        self.assertEqual(len(p.frames), len(packets) - 2)
        self.assertEqual([f[8:16] for f in p.frames], [bits([0xC3])] * (len(packets) - 2))

    def test_remote_pressed_by_hand(self):
        """NEC keys pressed by hand, from 0.3 to 1.5 s apart, the recording started 150 ms
        before the first and stopped 100 ms after the last: closer than the presses, still a
        pause, so each frame is whole and six give the check. Stopped in the middle of a
        seventh, that one is left out."""
        rnd = random.Random(16)
        keys = [(0x04, rnd.randrange(256)) for _ in range(7)]
        edges, t, level = [], 150_000, 1
        for k, key in enumerate(keys):
            for lv, us in nec_levels(key):
                if lv != level:
                    edges.append(round(t))
                    level = lv
                t += us
            edges.append(round(t))                           # 68 edges a frame
            level, t = 1, t + rnd.randrange(300_000, 1_500_000)
        whole = Capture(1, edges[:-68], (0, edges[-69] + 100_000))
        cut = Capture(1, edges[:-30], (0, edges[-31] + 300))
        for cap, n in ((whole, 6), (cut, 6)):
            p = infer_pulses(cap)
            self.assertEqual(p.frames, [bits([a, a ^ 255, c, c ^ 255]) for a, c in keys[:n]])
            self.assertEqual((p.crc.width, p.crc.poly), (8, 0x01))

    def test_uart_cut_at_both_ends(self):
        """Text sent 20 characters back to back at a time, recorded from the middle of the
        second character to the middle of the last (on the board: the first test of the chip's
        UART). The first and last bursts are left out."""
        fmt, bit = (8, None, 1, 1, 40), 40
        data = values(fmt, 17, 80)
        gaps = [300 if k % 20 == 19 else 0 for k in range(len(data))]
        bench = Bench(pullups=1 << PIN)
        bench.add(UartTx(PIN, bit, data, start=200, gap=gaps))
        bench.run(200 + (len(data) * 10 + 4 * 300) * bit)
        char = 10 * bit
        cut = (200 + char + char // 2, 200 + (len(data) * 10 + 3 * 300) * bit - char // 2)
        cap = self.recorded(bench.trace, cut, 1e6, 1e6)[f"D{PIN}"]
        p = infer_async(cap)
        Learn.check(self, p, fmt)
        got = decode(cap, p)
        self.assertTrue(any(got[k:k + 40] == data[20:60] for k in range(len(got))))

    def test_uart_back_to_back_cut(self):
        """ASCII text back to back with no pause, recorded from the middle of a character (on
        the board: 60 baud from SM 1): read from there, the first characters keep the format
        for a while (7 here) before a stop bit breaks it and the reading finds the start bits."""
        text = b"openPIO learns its own line. " * 4
        bench = Bench(pullups=1 << PIN)
        bench.add(UartTx(PIN, 40, list(text), start=200))
        bench.run(200 + len(text) * 400 + 4000)
        cap = self.recorded(bench.trace, (200 + 2 * 400 + 130, len(bench.trace)), 1e6, 1e6)[f"D{PIN}"]
        p = infer_async(cap)
        Learn.check(self, p, (8, None, 1, 1, 40))
        self.assertIn(b"learns its own line. " + b"openPIO learns its own line. " * 3, bytes(decode(cap, p)))

    def test_ps2_cut_at_both_ends(self):
        """A PS/2 keyboard recorded from the middle of a byte to the middle of another."""
        rnd = random.Random(15)
        keys = [rnd.randrange(256) for _ in range(50)]
        bench = Bench(pullups=1 << MOSI | 1 << SCK)
        bench.add(Ps2Device(SCK, MOSI, keys))
        bench.run(50 + 1080 * len(keys) + 300)
        caps = self.recorded(bench.trace, (50 + 400, 50 + 1080 * 49 + 400), 1e6, 1e6)
        (p,) = infer_clocked({MOSI: caps[f"D{MOSI}"], SCK: caps[f"D{SCK}"]})
        self.assertEqual((p.clock, p.data, p.cpol, p.cpha, p.crc.width), (SCK, MOSI, 1, 0, 1))
        self.assertEqual(p.frames, [ps2_bits(k) for k in keys[1:-1]])


if __name__ == "__main__":
    unittest.main()
