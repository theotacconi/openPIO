#!/usr/bin/env python3
"""I2S master on both SMs, judged by the reference I2S codec: SM 0 sends stereo samples to the
codec's DAC (16 bits a word, MSB one BCLK after LRCLK, data stable before BCLK rises), SM 1, in
lockstep from the same CTRL write, reads the codec's ADC samples; an empty TX FIFO sends silence
without stopping BCLK."""
import random
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
sys.path.insert(0, str(HERE.parents[1] / "sim" / "model"))
from bench import Bench                                  # noqa: E402
from devices import I2sCodec                             # noqa: E402

SDOUT, BCLK, LRCLK, SDIN = 0, 1, 2, 3                    # pins as in the programs
CLKDIV = 4


class I2s(unittest.TestCase):

    def test_both_directions(self):
        rng = random.Random(9)
        dac = [(rng.randrange(1 << 16), rng.randrange(1 << 16)) for _ in range(8)]
        adc = [(rng.randrange(1 << 16), rng.randrange(1 << 16)) for _ in range(8)]
        bench = Bench()
        bench.load(0, (HERE / "i2s_out.asm").read_text(), clkdiv_int=CLKDIV)
        bench.load(1, (HERE / "i2s_in.asm").read_text(), clkdiv_int=CLKDIV)
        codec = bench.add(I2sCodec(BCLK, LRCLK, SDOUT, SDIN, send=adc, setup=CLKDIV))
        bench.send(0, [s for pair in dac for s in pair])
        bench.start(0b11)                                # same clock: lockstep
        bench.run(12 * 32 * 2 * CLKDIV)                  # 8 frames, then 4 of silence
        self.assertEqual(bench.errors(), [])
        self.assertEqual(codec.received[:8], dac, "DAC")
        self.assertEqual(codec.received[8:11], [(0, 0)] * 3, "silence, BCLK running")
        got = bench.received[1]
        self.assertEqual(list(zip(got[0:16:2], got[1:16:2])), adc, "ADC")


if __name__ == "__main__":
    unittest.main()
