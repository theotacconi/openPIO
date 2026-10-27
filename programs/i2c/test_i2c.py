#!/usr/bin/env python3
"""I2C controller judged by the reference I2C target (open drain, pull-ups): writes, reads with
a repeated START, NACK from an absent target, clock stretching and lost arbitration."""
import random
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
sys.path.insert(0, str(HERE.parents[1] / "sim" / "model"))
from bench import Bench, Device                          # noqa: E402
from devices import I2cTarget                            # noqa: E402

SCL, SDA = 2, 3  # pins as in the program


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
        rng = random.Random(5)
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

    def test_absent_target_nacks(self):
        bench = setup(I2cTarget(SCL, SDA, 0x50))
        bench.send(0, [cmd(0x51 << 1, start=1, stop=1)])
        bench.run(400)
        self.assertEqual(bench.received[0], [(0x51 << 1) << 1 | 1])
        self.assertEqual(bench.errors(), [])

    def test_clock_stretching(self):
        target = I2cTarget(SCL, SDA, 0x50, stretch=40)
        bench = setup(target)
        bench.send(0, [cmd(0x50 << 1, start=1), cmd(0xA5, stop=1)])
        bench.run(1500)
        self.assertEqual(bench.errors(), [])
        self.assertEqual(target.transactions[0]["data"], [0xA5])
        lows = [n for lv, n in bench.runs(SCL) if lv == 0]
        self.assertGreaterEqual(max(lows), 40, "SCL held low by the target")

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


if __name__ == "__main__":
    unittest.main()
