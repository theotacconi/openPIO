#!/usr/bin/env python3
"""Self-check of the reference devices (devices.py) against ideal masters written here from
the protocol definitions, before they are used to judge openPIO programs.

    python3 test/shared/test_devices.py
"""
import random
import unittest

from bench import Bench, Device
from devices import CanBus, CanNode, I2cTarget, SpiSlave, UartRx, UartTx, can_crc15


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


def spi_master(mode, words, half=3, sck=0, mosi=1, bits=8, start=5):
    """Ideal SPI master: data out half a period before each sampling edge."""
    cpol, cpha = mode >> 1, mode & 1
    steps, t = [(0, {sck: cpol, mosi: 0})], start
    for w in words:
        for i in range(bits - 1, -1, -1):
            b = w >> i & 1
            if cpha == 0:
                steps += [(t, {mosi: b}), (t + half, {sck: 1 - cpol}), (t + 2 * half, {sck: cpol})]
            else:
                steps += [(t, {sck: 1 - cpol, mosi: b}), (t + half, {sck: cpol})]
            t += 2 * half
        t += 2 * half
    return Script(steps), t


def i2c_master(write, read=0, half=4, scl=0, sda=1):
    """Ideal I2C master (open drain): START, the `write` bytes, `read` bytes (ACK after each but
    the last, NACK after it), STOP. Data changes one clock after SCL falls."""
    low, rel = (lambda p: {p: 0}), (lambda p: {p: None})
    t = 10
    steps = [(t, low(sda)), (t + half, low(scl))]
    t += 2 * half

    def clock(bit):
        nonlocal t
        steps.append((t, rel(sda) if bit else low(sda)))
        steps.extend([(t + half, rel(scl)), (t + 2 * half, low(scl))])
        t += 2 * half + 1

    for b in write:
        for i in range(7, -1, -1):
            clock(b >> i & 1)
        clock(1)                                          # release SDA: the target ACKs
    for k in range(read):
        for _ in range(8):
            clock(1)                                      # release SDA: the target sends
        clock(1 if k == read - 1 else 0)                  # NACK the last byte, ACK the others
    steps += [(t, low(sda)), (t + half, rel(scl)), (t + 2 * half, rel(sda))]
    return Script(steps), t + 3 * half


def sda_at_scl_rises(bench, scl=0, sda=1):
    return [bench.trace[t] >> sda & 1 for t, lv in bench.edges(scl) if lv == 1]


class Uart(unittest.TestCase):

    def test_tx_to_rx(self):
        bench = Bench(pullups=1)
        data = [random.Random(1).randrange(256) for _ in range(20)]
        tx = bench.add(UartTx(0, 10, data, start=5, parity="odd", stop_bits=2))
        rx = bench.add(UartRx(0, 10, parity="odd", stop_bits=2))
        bench.run(tx.end + 20)
        self.assertEqual(rx.frames, data)
        self.assertEqual((rx.errors, rx.skew), ([], 0))

    def test_framing_error_is_seen(self):
        bench = Bench(pullups=1)
        tx = bench.add(UartTx(0, 8, [0x55, 0xAA], start=3, gap=2, stop_low={0}))
        rx = bench.add(UartRx(0, 8))
        bench.run(tx.end + 20)
        self.assertEqual(len(rx.errors), 1)


class Spi(unittest.TestCase):

    def test_all_modes(self):
        for mode in range(4):
            with self.subTest(mode=mode):
                bench = Bench()
                master, end = spi_master(mode, [0xA5, 0x3C, 0xFF])
                bench.add(master)
                slave = bench.add(SpiSlave(0, 1, 2, mode=mode, reply=[0x5A, 0xC3, 0x81]))
                bench.run(end + 10)
                self.assertEqual(slave.words, [0xA5, 0x3C, 0xFF])
                self.assertEqual(slave.errors, [])
                # what a master sees on MISO at the sampling edges (leading for CPHA 0)
                sample_level = (1 - (mode >> 1)) if (mode & 1) == 0 else mode >> 1
                bits = [bench.trace[t] >> 2 & 1 for t, lv in bench.edges(0) if lv == sample_level]
                words = [sum(b << (7 - i) for i, b in enumerate(bits[k:k + 8])) for k in (0, 8, 16)]
                self.assertEqual(words, [0x5A, 0xC3, 0x81], "MISO")

    def test_setup_violation_is_seen(self):
        bench = Bench()
        bench.add(Script([(0, {0: 0, 1: 0}), (5, {0: 1, 1: 1})]))   # MOSI and SCK together
        slave = bench.add(SpiSlave(0, 1, 2, mode=0, bits=1))
        bench.run(10)
        self.assertTrue(any("setup" in e for e in slave.errors))


class I2c(unittest.TestCase):

    def test_read(self):
        bench = Bench(pullups=0b11)
        master, end = i2c_master([0x50 << 1 | 1], read=2)
        bench.add(master)
        target = bench.add(I2cTarget(0, 1, 0x50, read_data=[0xAB, 0xCD], stretch=3))
        bench.run(end + 20)
        self.assertEqual(target.errors, [])
        self.assertEqual(target.transactions, [{"addr": 0x50, "rw": 1, "data": [0xAB, 0xCD], "acks": [1, 1, 0]}])
        bits = sda_at_scl_rises(bench)                    # what the master saw on SDA
        to_byte = lambda b: sum(x << (7 - i) for i, x in enumerate(b))
        self.assertEqual([to_byte(bits[9:17]), to_byte(bits[18:26])], [0xAB, 0xCD])
        self.assertEqual(bits[8], 0, "address ACK")

    def test_write_and_read(self):
        bench = Bench(pullups=0b11)
        master, end = i2c_master([0x50 << 1, 0x12, 0x34])
        bench.add(master)
        target = bench.add(I2cTarget(0, 1, 0x50, stretch=0))
        bench.run(end)
        self.assertEqual(target.errors, [])
        self.assertEqual(target.transactions, [{"addr": 0x50, "rw": 0, "data": [0x12, 0x34], "acks": [1, 1, 1]}])

    def test_other_address_is_not_acked(self):
        bench = Bench(pullups=0b11)
        master, end = i2c_master([0x51 << 1, 0x12])
        bench.add(master)
        target = bench.add(I2cTarget(0, 1, 0x50))
        bench.run(end)
        self.assertEqual(target.transactions[0]["acks"], [])


class Can(unittest.TestCase):

    def test_crc15_check_value(self):           # CRC-15/CAN of "123456789", MSB first
        bits = [b >> i & 1 for b in b"123456789" for i in range(7, -1, -1)]
        self.assertEqual(can_crc15(bits), 0x059E)

    def test_frame_ack_and_arbitration(self):
        a = CanNode(8, send=[(0x123, [0x11, 0xFF, 0x00])])
        b = CanNode(8, send=[(0x122, [0xAA])])            # lower ID: wins arbitration
        c = CanNode(8)
        bench = Bench()
        bench.add(CanBus([], [], [a, b, c]))
        bench.run(2500)
        self.assertEqual([n.errors for n in (a, b, c)], [[], [], []])
        self.assertEqual(c.frames, [{"id": 0x122, "data": [0xAA], "crc_ok": True},
                                    {"id": 0x123, "data": [0x11, 0xFF, 0x00], "crc_ok": True}])
        self.assertEqual((a.sent, b.sent), ([(0x123, True)], [(0x122, True)]), "both sent, acked")


if __name__ == "__main__":
    unittest.main()
