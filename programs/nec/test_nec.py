#!/usr/bin/env python3
"""NEC infrared remote receiver judged against the reference remote (the IR receiver module's
output): frames with random address and command, the repeat code, a lone 3 ms burst ignored,
at the nominal timing and with every time 10 % longer or shorter. At the nominal timing sigrok's
NEC decoder reads the same keys and the repeat code (it takes no frame 10 % off)."""
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
from sigrok import ir_nec as sigrok_nec                  # noqa: E402

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
                if scale == 1.0:                         # 1 clock = 1 us
                    def key(a, c):
                        return [f"Address: 0x{a:02X}", f"Address#: 0x{a ^ 0xFF:02X}",
                                f"Command: 0x{c:02X}", f"Command#: 0x{c ^ 0xFF:02X}"]
                    self.assertEqual(sigrok_nec(bench, IR, 1e6),
                                     key(*keys[0]) + ["Repeat code"] + key(*keys[1]), "sigrok")


if __name__ == "__main__":
    unittest.main()
