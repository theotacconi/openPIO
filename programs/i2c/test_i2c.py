#!/usr/bin/env python3
"""I2C controller judged by the reference I2C target (open drain, pull-ups): writes, reads with
a repeated START, NACK from an absent target, clock stretching and lost arbitration; the bus
timing of UM10204's Table 10 at the top SCL of Standard-mode, Fast-mode and Fast-mode Plus, with
a target as slow as each allows; random settings inside that. I2C target against the reference
I2C controller: writes, reads after a repeated START answered by the host, other addresses
ignored, SCL held low while the host has not answered, the bytes a NACK leaves dropped, a STOP
or a repeated START inside a byte; at the top SCL of each mode at the largest divider the
target promises, every byte valid within tVD;DAT; random settings inside that. sigrok's I2C
decoder reads the same conditions, addresses, bytes and acknowledges."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
sys.path.insert(0, str(HERE.parents[1] / "sim" / "model"))
from bench import Bench, Device, Script                          # noqa: E402
from devices import I2C_FSCL, I2C_TIMING, I2cTarget, I2cTiming   # noqa: E402
from devices import I2cController as Controller           # noqa: E402  (the class below is the program's test)
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


TARGET = 0x50    # i2c_target.asm's address (Y = 0x100 | address << 1)
DIVS = {"fm+": (1, 2), "fm": (2, 4), "sm": (4, 18)}     # the CLKDIVs the target promises at 50 MHz


def run_target(transactions, mode="fm+", div=1.0, hold=1, reads=(), delay=0, extra=(),
               gap=0, clocks=None):
    """i2c_target.asm on SM 0 at CLKDIV `div` against the reference controller running
    `transactions` at the top SCL of `mode` (50 clocks a microsecond), judged by I2cTiming. The
    host loads the bytes of each read (`reads`, one list per read, in order, then `extra`)
    `delay` clocks after the read's address byte reaches it. Returns (bench, controller)."""
    us = 50
    bench = Bench(pullups=1 << SCL | 1 << SDA)
    bench.load(0, (HERE / "i2c_target.asm").read_text(), clkdiv_int=int(div),
               clkdiv_frac=round(div % 1 * 256))

    def held():                                      # the target pulls SCL low
        out, oe = bench.chip.pins
        return oe >> SCL & 1 and not out >> SCL & 1
    ctl = bench.add(Controller(SCL, SDA, us, transactions, mode=mode, hold=hold, gap=gap, held=held))
    bench.add(I2cTiming(SCL, SDA, us, mode))
    bench.start(1)
    reads, due, seen = [list(r) for r in reads], [], [0]

    def host():
        words = bench.received[0]
        for w in words[seen[0]:]:
            if w >> 8 and w & 1:                     # a read's address byte
                due.append(bench.t + delay)
        seen[0] = len(words)
        while due and bench.t >= due[0]:
            due.pop(0)
            bench.send(0, (reads.pop(0) if reads else []) + list(extra))
        return ctl.done
    bench.run(clocks or 400_000, until=host)
    return bench, ctl


def expected(transactions):
    """(the controller's results, the target's RX FIFO words) for transactions that the target
    answers in full: every segment to TARGET ACKed, reads given their bytes."""
    results, words = [], []
    for segments in transactions:
        for addr, what in segments:
            read = isinstance(what, int)
            if addr != TARGET:
                results.append({"addr": addr, "rw": int(read), "data": [], "acks": [0]})
                break
            words.append(0x100 | addr << 1 | read)
            if read:
                results.append({"addr": addr, "rw": 1, "data": None, "acks": [1]})
            else:
                results.append({"addr": addr, "rw": 0, "data": what, "acks": [1] * (len(what) + 1)})
                words += what
    return results, words


class I2cTargetProgram(unittest.TestCase):

    def check(self, transactions, reads, **kw):
        bench, ctl = run_target(transactions, reads=reads, **kw)
        results, words = expected(transactions)
        it = iter(reads)
        for r in results:
            if r["data"] is None:
                r["data"] = list(next(it))
        self.assertTrue(ctl.done, "the controller finished")
        self.assertEqual(bench.errors(), [])
        self.assertEqual(ctl.results, results)
        self.assertEqual(bench.received[0], words)
        self.assertEqual(bench.wave(SCL)[-1] + bench.wave(SDA)[-1], "11", "bus released")
        return bench, ctl

    def test_write_and_read(self):
        """A write; a register written, a repeated START and a read the host answers; another
        address, not acknowledged and not reported; a read of one byte."""
        rng = seeded(41)
        wdata = [rng.randrange(256) for _ in range(5)]
        rdata = [rng.randrange(256) for _ in range(4)]
        trans = [[(TARGET, wdata)], [(TARGET, [0x10]), (TARGET, 4)], [(0x51, [0x99])], [(TARGET, 1)]]
        bench, _ = self.check(trans, [rdata, [0x3C]])
        self.assertEqual(sigrok_i2c(bench, SCL, SDA),
                         ["Start", "Address write: 50", "ACK"]
                         + [x for b in wdata for x in (f"Data write: {b:02X}", "ACK")] + ["Stop"]
                         + ["Start", "Address write: 50", "ACK", "Data write: 10", "ACK",
                            "Start repeat", "Address read: 50", "ACK"]
                         + [x for b in rdata[:-1] for x in (f"Data read: {b:02X}", "ACK")]
                         + [f"Data read: {rdata[-1]:02X}", "NACK", "Stop"]
                         + ["Start", "Address write: 51", "NACK", "Stop"]
                         + ["Start", "Address read: 50", "ACK", "Data read: 3C", "NACK", "Stop"],
                         "sigrok")

    def test_clock_stretching(self):
        """The host answers a read 3000 clocks after its address byte: SCL is held low until
        then, and the controller reads the bytes."""
        bench, _ = self.check([[(TARGET, [0x01]), (TARGET, 2)]], [[0xDE, 0xAD]], delay=3000)
        self.assertGreater(max(n for lv, n in bench.runs(SCL) if lv == 0), 2900, "SCL held low")

    def test_nack_drops_the_rest(self):
        """The host loads 3 bytes more than a read takes: the controller's NACK drops them, the
        next read gets its own."""
        self.check([[(TARGET, 2)], [(TARGET, 2)]], [[0x11, 0x22], [0x33, 0x44]], extra=[0xEE, 0xEF, 0xF0])

    def test_stop_and_start_inside_a_byte(self):
        """A controller that gives up in the middle of a byte: a STOP after 3 bits of a write,
        then a repeated START after 4 bits of the next byte. The target follows each: the cut
        bytes are not reported, the next address byte is, every address ACKed. SDA changes on
        the clock SCL falls (UM10204 allows a hold time of 0)."""
        h, t, steps, acks = 25, 50, [], []
        scl_high = True

        def drive(*changes):
            """Each (pin, level) h clocks after the one before; a level of 1 releases the pin."""
            nonlocal t
            for pin, lv in changes:
                steps.append((t, {pin: None if lv else 0}))
                t += h

        parts = ["S", "10100000", "z", "101", "P",
                 "S", "10100000", "z", "01011010", "z", "1100", "S", "10100000", "z", "11000011", "z", "P"]
        for part in parts:
            if part == "S":                                  # START, or a repeated one
                if not scl_high:
                    drive((SDA, 1), (SCL, 1))
                drive((SDA, 0), (SCL, 0))
            elif part == "P":                                # STOP
                drive((SDA, 0), (SCL, 1), (SDA, 1))
            else:                                            # bits; z: SDA released, the target's turn
                for b in part:
                    if b == "z":
                        acks.append(t + h + h // 2)
                    drive((SDA, b != "0"), (SCL, 1))
                    steps.append((t, {SCL: 0}))
            scl_high = part == "P"
        bench = Bench(pullups=1 << SCL | 1 << SDA)
        bench.load(0, (HERE / "i2c_target.asm").read_text())
        bench.add(Script(steps))
        bench.start(1)
        bench.run(t + 100)
        self.assertEqual(bench.errors(), [])
        self.assertEqual(bench.received[0], [0x1A0, 0x1A0, 0x5A, 0x1A0, 0xC3])
        self.assertEqual("".join(bench.wave(SDA)[a] for a in acks), "00000", "every ACK")

    def test_timing(self):
        """At the top SCL of each mode, at the smallest and the largest divider the target
        promises, a controller changing SDA a clock after SCL falls and a host answering a read
        late: every byte acknowledged, every bit the target sends valid within tVD;DAT, the
        first bit of the stretched read set up tSU;DAT before SCL rises, the bus timing of
        Table 10."""
        for mode, divs in DIVS.items():
            for div in divs:
                with self.subTest(mode=mode, div=div):
                    rng = seeded(42)
                    data = [rng.randrange(256) for _ in range(3)]
                    self.check([[(TARGET, data)], [(TARGET, [0x20]), (TARGET, 3)]], [[0xA5, 0x00, 0xFF]],
                               mode=mode, div=div, delay=2000)

    def test_sweep(self):
        """Random settings inside what i2c_target.asm promises: a mode, a divider in the
        mode's range (fractional inside it), a controller changing SDA 1 clock to tVD;DAT after SCL
        falls, back to back or apart; writes, reads after a repeated START, reads, other
        addresses, bytes rich in 0x00 and 0xFF; the host answering at once or late, sometimes
        with bytes too many."""
        rnd = seeded(43)

        def byte():
            return rnd.choice([0x00, 0xFF, rnd.randrange(256)])
        for trial in range(4):
            mode = rnd.choice(list(DIVS))
            lo, hi = DIVS[mode]
            div = rnd.randrange(lo, hi + 1)
            div += rnd.choice([0, rnd.randrange(256)]) / 256 if div < hi else 0
            hold = rnd.randrange(1, int(I2C_TIMING[mode]["tVD;DAT"] * 50 * 0.5) + 1)
            trans, reads = [], []
            for _ in range(4):
                data = [byte() for _ in range(rnd.randrange(1, 6))]
                kind = rnd.choice(["write", "register", "read", "other"])
                if kind == "write":
                    trans.append([(TARGET, data)])
                elif kind == "other":
                    trans.append([(TARGET ^ rnd.randrange(1, 0x80), data)])
                else:
                    trans.append(([(TARGET, [byte()])] if kind == "register" else []) + [(TARGET, len(data))])
                    reads.append(data)
            with self.subTest(trial=trial, mode=mode, div=div, hold=hold):
                self.check(trans, reads, mode=mode, div=div, hold=hold,
                           delay=rnd.choice([0, rnd.randrange(2000)]),
                           extra=rnd.choice([[], [0x55]]),
                           gap=lambda k: rnd.choice([0, rnd.randrange(500)]))


if __name__ == "__main__":
    unittest.main()
