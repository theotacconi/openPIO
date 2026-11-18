#!/usr/bin/env python3
"""I2C controller judged by the reference I2C target (open drain, pull-ups): writes, reads with
a repeated START, NACK from an absent target, clock stretching and lost arbitration; the bus
timing of UM10204's Table 10 at the top SCL of Standard-mode, Fast-mode and Fast-mode Plus, with
a target as slow as each allows; random settings inside that. sigrok's I2C decoder reads the
same conditions, addresses, bytes and acknowledges."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
sys.path.insert(0, str(HERE.parents[1] / "sim" / "model"))
from bench import Bench, Device                          # noqa: E402
from devices import I2C_FSCL, I2C_TIMING, I2cTarget, I2cTiming   # noqa: E402
from sigrok import i2c as sigrok_i2c                     # noqa: E402
from seeds import seeded                                 # noqa: E402

SCL, SDA = 2, 3  # pins as in the program
STROBES = 20     # a bit at least: SCL up to strobes / 20


def cmd(data=0, start=0, read=0, nack=0, stop=0):
    return start << 15 | read << 14 | (data & 0xFF) << 6 | nack << 5 | stop << 4


def setup(target=None, clkdiv=1):
    bench = Bench(pullups=1 << SCL | 1 << SDA)
    bench.load(0, (HERE / "i2c_master.asm").read_text(), clkdiv_int=clkdiv)
    if target:
        bench.add(target)
    bench.start(1)
    return bench


class I2cController(unittest.TestCase):

    def test_write_then_read_with_repeated_start(self):
        rng = seeded(5)
        wdata = [rng.randrange(256) for _ in range(6)]
        rdata = [rng.randrange(256) for _ in range(5)]
        target = I2cTarget(SCL, SDA, 0x50, read_data=rdata)
        bench = setup(target)
        cmds = [cmd(0x50 << 1, start=1)] + [cmd(b) for b in wdata]
        cmds += [cmd(0x50 << 1 | 1, start=1)] + [cmd(read=1) for _ in rdata[:-1]]
        cmds += [cmd(read=1, nack=1, stop=1)]
        bench.send(0, cmds)
        bench.run(4000)
        self.assertEqual(bench.errors(), [])
        self.assertEqual(target.transactions, [
            {"addr": 0x50, "rw": 0, "data": wdata, "acks": [1] * 7},
            {"addr": 0x50, "rw": 1, "data": rdata, "acks": [1, 1, 1, 1, 1, 0]},
        ])
        results = bench.received[0]
        self.assertEqual(results[:7], [(0x50 << 1) << 1] + [b << 1 for b in wdata], "bytes seen, ACK 0")
        self.assertEqual(results[8:], [b << 1 for b in rdata[:-1]] + [rdata[-1] << 1 | 1], "reads, NACK last")
        self.assertEqual(bench.wave(SCL)[-1] + bench.wave(SDA)[-1], "11", "bus idle after STOP")
        self.assertEqual(sigrok_i2c(bench, SCL, SDA),
                         ["Start", "Address write: 50", "ACK"]
                         + [x for b in wdata for x in (f"Data write: {b:02X}", "ACK")]
                         + ["Start repeat", "Address read: 50", "ACK"]
                         + [x for b in rdata[:-1] for x in (f"Data read: {b:02X}", "ACK")]
                         + [f"Data read: {rdata[-1]:02X}", "NACK", "Stop"], "sigrok")

    def test_absent_target_nacks(self):
        bench = setup(I2cTarget(SCL, SDA, 0x50))
        bench.send(0, [cmd(0x51 << 1, start=1, stop=1)])
        bench.run(400)
        self.assertEqual(bench.received[0], [(0x51 << 1) << 1 | 1])
        self.assertEqual(bench.errors(), [])
        self.assertEqual(sigrok_i2c(bench, SCL, SDA), ["Start", "Address write: 51", "NACK", "Stop"],
                         "sigrok")

    def test_clock_stretching(self):
        target = I2cTarget(SCL, SDA, 0x50, stretch=40)
        bench = setup(target)
        bench.send(0, [cmd(0x50 << 1, start=1), cmd(0xA5, stop=1)])
        bench.run(1500)
        self.assertEqual(bench.errors(), [])
        self.assertEqual(target.transactions[0]["data"], [0xA5])
        lows = [n for lv, n in bench.runs(SCL) if lv == 0]
        self.assertGreaterEqual(max(lows), 40, "SCL held low by the target")
        self.assertEqual(sigrok_i2c(bench, SCL, SDA),
                         ["Start", "Address write: 50", "ACK", "Data write: A5", "ACK", "Stop"], "sigrok")

    def test_lost_arbitration(self):
        class Rival(Device):                    # another controller sending 0x00 to the same bus
            def tick(self, t, levels):
                self.drive = {SDA: 0} if t > 30 and levels >> SCL & 1 == 0 else self.drive
        bench = setup()
        bench.add(Rival())
        bench.send(0, [cmd(0xFF, start=1, stop=1)])
        bench.run(400)
        sm = bench.chip.sms[0]
        self.assertTrue(sm.irq_flags[0], "IRQ 0: arbitration lost")
        out, oe = bench.chip.pins
        self.assertEqual(oe >> SCL & 3, 0, "both lines released")

    def transfers(self, mode, div, rnd, valid=None, stretch=None, n=4):
        """n random transactions (a write; a write of a register, a repeated START and a read;
        an address nobody answers) at the top SCL of `mode` (strobes / STROBES at CLKDIV
        `div`), judged by I2cTiming; the target changes SDA `valid` clocks after SCL falls (the
        mode's tVD;DAT by default) and holds SCL low stretch() clocks after each byte."""
        us = STROBES * div * I2C_FSCL[mode]                 # clocks a microsecond
        addr = rnd.randrange(8, 0x78)
        cmds, results, expect, reads = [], [], [], []
        for _ in range(n):
            kind = rnd.choice(["write", "read", "absent"])
            data = [rnd.choice([0x00, 0xFF, rnd.randrange(256)]) for _ in range(rnd.randrange(1, 9))]
            if kind == "write":
                cmds += [cmd(addr << 1, start=1)] + [cmd(b) for b in data[:-1]] + [cmd(data[-1], stop=1)]
                results += [addr << 2] + [b << 1 for b in data]
                expect.append({"addr": addr, "rw": 0, "data": data, "acks": [1] * (len(data) + 1)})
            elif kind == "read":
                reg = rnd.randrange(256)
                cmds += [cmd(addr << 1, start=1), cmd(reg), cmd(addr << 1 | 1, start=1)]
                cmds += [cmd(read=1) for _ in data[:-1]] + [cmd(read=1, nack=1, stop=1)]
                results += [addr << 2, reg << 1, (addr << 1 | 1) << 1] + [b << 1 for b in data[:-1]]
                results += [data[-1] << 1 | 1]
                expect += [{"addr": addr, "rw": 0, "data": [reg], "acks": [1, 1]},
                           {"addr": addr, "rw": 1, "data": data, "acks": [1] * len(data) + [0]}]
                reads += data
            else:
                other = addr ^ rnd.randrange(1, 0x80)
                cmds.append(cmd(other << 1, start=1, stop=1))
                results.append(other << 2 | 1)
                expect.append({"addr": other, "rw": 0, "data": [], "acks": []})
        if valid is None:
            valid = int(I2C_TIMING[mode]["tVD;DAT"] * us)
        target = I2cTarget(SCL, SDA, addr, read_data=reads, valid=valid, stretch=stretch or 0)
        judge = I2cTiming(SCL, SDA, us, mode)
        bench = Bench(pullups=1 << SCL | 1 << SDA)
        bench.load(0, (HERE / "i2c_master.asm").read_text(), clkdiv_int=int(div),
                   clkdiv_frac=round(div % 1 * 256))
        bench.add(target)
        bench.add(judge)
        bench.start(1)
        bench.send(0, cmds)
        bench.run(len(cmds) * 400 * round(div + 1) + 100_000, until=lambda: len(bench.received[0]) == len(results))
        self.assertEqual(bench.errors(), [])
        self.assertEqual(target.transactions, expect)
        self.assertEqual(bench.received[0], results)

    def test_timing(self):
        """UM10204 Table 10 at the top SCL of each mode, at strobes / 20: every SCL low and
        high, data setup, START and STOP setup and hold, the bus free time; a target that
        changes SDA as late as the mode allows."""
        for mode in ("sm", "fm", "fm+"):
            with self.subTest(mode=mode):
                self.transfers(mode, 10, seeded(27))

    def test_sweep(self):
        """Random settings inside what i2c_master.asm promises: a mode, a divider from 1 to 12
        (fractional from 2) at the mode's top SCL, a target changing SDA up to the mode's tVD;DAT after
        SCL falls (at it in the first two trials) and stretching SCL after some bytes, writes,
        reads after a repeated START and absent addresses rich in 0x00 and 0xFF. Every byte,
        acknowledge and Table 10 time right."""
        rnd = seeded(28)
        for trial in range(4):
            mode = rnd.choice(["sm", "fm", "fm+"])
            div = rnd.randrange(1, 13)
            div += rnd.choice([0, rnd.randrange(256)]) / 256 if div >= 2 else 0
            us = STROBES * div * I2C_FSCL[mode]
            vd = I2C_TIMING[mode]["tVD;DAT"] * us
            valid = int(vd) if trial < 2 else rnd.randrange(int(vd) + 1)
            with self.subTest(trial=trial, mode=mode, div=div, valid=valid):
                self.transfers(mode, div, rnd, valid, lambda: rnd.choice([0, 0, rnd.randrange(1, 3 * STROBES * round(div + 1))]))


if __name__ == "__main__":
    unittest.main()
