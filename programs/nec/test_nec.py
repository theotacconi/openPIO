#!/usr/bin/env python3
"""NEC infrared remote receiver judged against the reference remote (the IR receiver module's
output): frames with random address and command, the repeat code, a lone 3 ms burst ignored,
at the nominal timing and with every time 10 % longer or shorter."""
import random
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
sys.path.insert(0, str(HERE.parents[1] / "sim" / "model"))
from bench import Bench                                  # noqa: E402
from chip import IRQ                                     # noqa: E402
from devices import NecRemote                            # noqa: E402

IR = 0                                                   # pin as in the program


class Nec(unittest.TestCase):

    def test_frames_repeat_and_noise(self):
        rng = random.Random(6)
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


if __name__ == "__main__":
    unittest.main()
