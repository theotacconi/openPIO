#!/usr/bin/env python3
"""NEC infrared remote receiver judged against the reference remote (the IR receiver module's
output): frames with random address and command, the repeat code, a lone 3 ms burst ignored,
at the nominal timing and with every time 10 % longer or shorter; random settings inside what
the receiver promises (a divider, the remote 10 % fast or slow, the module's marks longer or
shorter, jittered edges). At the nominal timing sigrok's NEC decoder reads the same keys and the
repeat code (it takes no frame 10 % off)."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
sys.path.insert(0, str(HERE.parents[1] / "sim" / "model"))
from bench import Bench                                  # noqa: E402
from chip import IRQ                                     # noqa: E402
from devices import NecRemote                            # noqa: E402
from sigrok import ir_nec as sigrok_nec                  # noqa: E402
from seeds import seeded                                 # noqa: E402

IR = 0                                                   # pin as in the program


class Nec(unittest.TestCase):

    def test_frames_repeat_and_noise(self):
        rng = seeded(6)
        for scale in (1.0, 1.1, 0.9):
            with self.subTest(scale=scale):
                keys = [(rng.randrange(256), rng.randrange(256)) for _ in range(2)]
                bench = Bench(pullups=1 << IR)
                bench.load(0, (HERE / "nec_rx.asm").read_text())
                remote = bench.add(NecRemote(IR, [("glitch", 3000), keys[0], "repeat", keys[1]],
                                             us=scale, start=1000))
                bench.start(1)
                repeats = 0
                while bench.t < remote.end:
                    bench.run(1000)
                    if bench.chip.reg_read(0, IRQ):
                        repeats += 1
                        bench.write(0, IRQ, 1)
                self.assertEqual(bench.errors(), [])
                self.assertEqual(bench.received[0], [w for a, c in keys for w in (a | (a ^ 0xFF) << 8,
                                                                                   c | (c ^ 0xFF) << 8)])
                self.assertEqual(repeats, 1)
                if scale == 1.0:                         # 1 clock = 1 us
                    def key(a, c):
                        return [f"Address: 0x{a:02X}", f"Address#: 0x{a ^ 0xFF:02X}",
                                f"Command: 0x{c:02X}", f"Command#: 0x{c ^ 0xFF:02X}"]
                    self.assertEqual(sigrok_nec(bench, IR, 1e6),
                                     key(*keys[0]) + ["Repeat code"] + key(*keys[1]), "sigrok")

    def test_sweep(self):
        """Random settings inside what nec_rx.asm promises: a divider of 1 to 4 (fractional; 1
        strobe is 1 us), every time of the remote 10 % longer or shorter, the receiver module's
        marks 160 us longer or shorter (a TSOP's output pulse width error, its spaces as much
        shorter or longer), each edge up to 20 us off, keys 0x00 and 0xFF among random ones,
        frames 2 to 40 ms apart. Every key read and the repeat code seen. Measured, at 10 %
        slow: marks 240 us short to 400 us long pass (200 us short with the bit sampled at
        838 us, as before); at 10 % fast, 400 us short to 400 us long."""
        rnd = seeded(20)
        for trial in range(3):
            clkdiv, frac = rnd.randrange(1, 5), rnd.choice([0, rnd.randrange(256)])
            scale = [0.9, 1.1, rnd.uniform(0.9, 1.1)][trial]          # the limits too
            mark = [-160, 160, rnd.uniform(-160, 160)][rnd.randrange(3)]
            keys = [(rnd.choice([0x00, 0xFF, rnd.randrange(256)]), rnd.choice([0x00, 0xFF, rnd.randrange(256)]))
                    for _ in range(3)]
            us = clkdiv + frac / 256
            with self.subTest(trial=trial, clkdiv=clkdiv, frac=frac, scale=f"{scale:.3f}", mark=f"{mark:+.0f}"):
                bench = Bench(pullups=1 << IR)
                bench.load(0, (HERE / "nec_rx.asm").read_text(), clkdiv_int=clkdiv, clkdiv_frac=frac)
                remote = bench.add(NecRemote(IR, [keys[0], "repeat", keys[1], keys[2]], us=us * scale,
                                             start=1000, gap=rnd.uniform(2000, 40000),
                                             mark=mark / scale, jitter=20 / scale,   # real us
                                             seed=rnd.randrange(1 << 30)))
                bench.start(1)
                repeats = 0
                while bench.t < remote.end:
                    bench.run(1000)
                    if bench.chip.reg_read(0, IRQ):
                        repeats += 1
                        bench.write(0, IRQ, 1)
                self.assertEqual(bench.errors(), [])
                self.assertEqual(bench.received[0], [w for a, c in keys for w in (a | (a ^ 0xFF) << 8,
                                                                                   c | (c ^ 0xFF) << 8)])
                self.assertEqual(repeats, 1)


if __name__ == "__main__":
    unittest.main()
