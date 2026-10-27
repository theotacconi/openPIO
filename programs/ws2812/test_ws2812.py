#!/usr/bin/env python3
"""WS2812B LED chain driven by the program and judged by the reference chain: every high and low
time within the datasheet's +-150 ns, at 48 MHz (CLKDIV 6) and 50 MHz (fractional CLKDIV 6.25);
two frames latched by the 50 us low gap when the FIFO runs empty."""
import random
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
sys.path.insert(0, str(HERE.parents[1] / "sim" / "model"))
from bench import Bench                                  # noqa: E402
from devices import Ws2812Chain                          # noqa: E402

DIN, LEDS = 0, 4                                         # pin as in the program


def words(colours):
    """(G, R, B) per LED -> 16-bit words, MSB first."""
    bits = "".join(f"{g:08b}{r:08b}{b:08b}" for g, r, b in colours)
    bits += "0" * (-len(bits) % 16)
    return [int(bits[i:i + 16], 2) for i in range(0, len(bits), 16)]


class Ws2812(unittest.TestCase):

    def test_two_frames(self):
        rng = random.Random(5)
        for mhz, clkdiv_int, clkdiv_frac in ((48, 6, 0), (50, 6, 64)):
            with self.subTest(mhz=mhz):
                frames = [[tuple(rng.randrange(256) for _ in range(3)) for _ in range(LEDS)] for _ in range(2)]
                bench = Bench()
                bench.load(0, (HERE / "ws2812.asm").read_text(), clkdiv_int=clkdiv_int, clkdiv_frac=clkdiv_frac)
                chain = bench.add(Ws2812Chain(DIN, LEDS, 1000 / mhz))
                bench.send(0, words(frames[0]))
                bench.start(1)
                bench.run(9000)                          # 96 bits, then the latch
                bench.send(0, words(frames[1]))
                bench.run(9000)
                self.assertEqual(bench.errors(), [])
                self.assertEqual(chain.frames, frames)


if __name__ == "__main__":
    unittest.main()
