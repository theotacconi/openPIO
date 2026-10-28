#!/usr/bin/env python3
"""Self-check of the reference devices (devices.py) against ideal masters written here from
the protocol definitions, before they are used to judge openPIO programs.

    python3 test/shared/test_devices.py
"""
import random
import unittest

from bench import Bench, Script
from devices import (BmcRx, BmcTx, DaliBus, DaliGear, WiredAndBus, CanNode, HdlcRx, I2cTarget,
                     I2sCodec, SpiSlave, UartRx, UartTx, Ws2812Chain, can_crc15, crc16_modbus,
                     crc16_x25)


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
        bench.add(WiredAndBus([], [], [a, b, c]))
        bench.run(2500)
        self.assertEqual([n.errors for n in (a, b, c)], [[], [], []])
        self.assertEqual(c.frames, [{"id": 0x122, "data": [0xAA], "crc_ok": True},
                                    {"id": 0x123, "data": [0x11, 0xFF, 0x00], "crc_ok": True}])
        self.assertEqual((a.sent, b.sent), ([(0x123, True)], [(0x122, True)]), "both sent, acked")


class Ws2812(unittest.TestCase):

    def wave(self, bits, high=(8, 16), low=(17, 9)):
        """Bits at 50 ns a clock: nominal 400 / 800 ns high, 850 / 450 ns low; then a latch."""
        steps, t = [(0, {0: 0})], 10
        for b in bits:
            steps += [(t, {0: 1}), (t + high[b], {0: 0})]
            t += high[b] + low[b]
        return Script(steps), t + 1100

    def test_two_leds_latch(self):
        colours = [(0x12, 0x34, 0x56), (0xFF, 0x00, 0x81)]
        bits = [c >> i & 1 for led in colours for c in led for i in range(7, -1, -1)]
        bench = Bench()
        script, end = self.wave(bits)
        bench.add(script)
        chain = bench.add(Ws2812Chain(0, 2, 50))
        bench.run(end)
        self.assertEqual((chain.frames, chain.errors), ([colours], []))

    def test_high_out_of_tolerance(self):
        bench = Bench()
        script, end = self.wave([1] * 24, high=(8, 12))   # 600 ns: neither a 0 nor a 1
        bench.add(script)
        chain = bench.add(Ws2812Chain(0, 1, 50))
        bench.run(end)
        self.assertTrue(chain.errors)


class Hdlc(unittest.TestCase):

    def test_crc16_x25_check_value(self):
        self.assertEqual(crc16_x25(list(b"123456789")), 0x906E)

    def frame_bits(self, data, fcs):
        bits, ones = [], 0
        for b in [byte >> i & 1 for byte in data + [fcs & 0xFF, fcs >> 8] for i in range(8)]:
            bits.append(b)
            ones = ones + 1 if b else 0
            if ones == 5:
                bits.append(0)                            # zero insertion
                ones = 0
        flag = [0, 1, 1, 1, 1, 1, 1, 0]
        return [1] * 8 + flag + bits + flag + [1] * 16

    def run_line(self, bits, bit=8):
        steps, level, t = [], 1, 5
        for b in bits:                                    # NRZI: a 0 is a transition
            level ^= 1 - b
            steps.append((t, {0: level}))
            t += bit
        bench = Bench()
        bench.add(Script(steps))
        rx = bench.add(HdlcRx(0, bit))
        bench.run(t + 10)
        return rx

    def test_frames(self):
        data = [0x7E, 0xFF, 0x00, 0x1F]
        rx = self.run_line(self.frame_bits(data, crc16_x25(data)) + self.frame_bits([0x55], 0x1234))
        self.assertEqual(rx.errors, [])
        self.assertEqual(rx.frames, [{"data": data, "fcs_ok": True}, {"data": [0x55], "fcs_ok": False}])


class I2s(unittest.TestCase):

    def test_ideal_master(self):
        """BCLK 8 clocks, data out after falling edges, LRCLK one bit ahead of the MSB."""
        sent, adc = [(0x8001, 0x1234), (0xFFFF, 0x0F0F)], [(0xA5A5, 0x0001), (0x7FFF, 0x8000)]
        bits = [(lr, v >> i & 1) for left, right in sent for lr, v in ((0, left), (1, right))
                for i in range(15, -1, -1)]
        # bit slots (LRCLK, SD): LRCLK is the channel of the next bit; first an empty right word
        slots = [(1, 0), (0, 0)] + [(bits[k + 1][0] if k + 1 < len(bits) else 0, bits[k][1])
                                    for k in range(len(bits))]
        steps, t = [(0, {0: 0, 1: 1, 2: 0})], 10          # BCLK 0, LRCLK 1, SD 2
        for lr, sd in slots:
            steps += [(t, {0: 0, 1: lr, 2: sd}), (t + 4, {0: 1})]
            t += 8
        bench = Bench()
        bench.add(Script(steps))
        codec = bench.add(I2sCodec(0, 1, 2, 3, send=adc))
        bench.run(t + 10)
        self.assertEqual((codec.received, codec.errors), (sent, []))
        got = [bench.trace[r] >> 3 & 1 for r, lv in bench.edges(0) if lv == 1][2:66]
        words = [sum(b << (15 - i) for i, b in enumerate(got[k:k + 16])) for k in range(0, 64, 16)]
        self.assertEqual(words, [s for pair in adc for s in pair], "ADC on SDOUT at the rising edges")


class Modbus(unittest.TestCase):

    def test_crc16_modbus_check_value(self):
        self.assertEqual(crc16_modbus(list(b"123456789")), 0x4B37)


class Bmc(unittest.TestCase):

    def test_tx_to_rx(self):
        frames = [[0, 1] * 8 + [1, 1, 0, 0, 1], [1, 0, 0, 1, 1, 1, 0]]
        bench = Bench()
        tx = bench.add(BmcTx(0, 16, frames, start=10))
        rx = bench.add(BmcRx(0, 16))
        bench.run(tx.end + 50)
        self.assertEqual((rx.frames, rx.errors), (frames, []))


class Dali(unittest.TestCase):

    def test_ideal_master_and_answer(self):
        """A forward frame from an ideal master (TX pin high = bus low), a query answered after
        10 half-bits; the backward frame read back from the bus in the middle of each half."""
        te, frame = 20, 0xA5C3
        bits = [1] + [frame >> i & 1 for i in range(15, -1, -1)]
        steps, t = [(0, {0: 0})], 30
        for b in bits:                                    # bus low = pin high
            steps += [(t, {0: b}), (t + te, {0: 1 - b})]
            t += 2 * te
        steps.append((t, {0: 0}))
        gear = DaliGear(te, answers={frame: 0x5A}, settle=10 * te)
        bench = Bench()
        bench.add(Script(steps))
        bus = bench.add(DaliBus(0, 1, [gear, DaliGear(te)]))
        bench.run(t + 60 * te)
        self.assertEqual((gear.frames, bench.errors()), ([frame], []))
        t0 = t + 10 * te
        halves = [bus.wave[t0 + te // 2 + k * te] for k in range(18)]
        self.assertEqual(halves, [v for b in [1, 0, 1, 0, 1, 1, 0, 1, 0] for v in (1 - b, b)])
        self.assertEqual([bench.trace[t] >> 1 & 1 for t in (5, t0 + te // 2)], [0, 1], "RX inverted")


if __name__ == "__main__":
    unittest.main()
