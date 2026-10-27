#!/usr/bin/env python3
"""JTAG cycle engine driving a reference IEEE 1149.1 TAP: IDCODE after reset, IR and DR scans,
a USER register written and read back, BYPASS."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
sys.path.insert(0, str(HERE.parents[1] / "sim" / "model"))
from bench import Bench                                  # noqa: E402
from chip import PIN_MASK                                # noqa: E402
from devices import JtagTap                              # noqa: E402

TDI, TMS, TCK, TDO = 0, 1, 2, 3
IDCODE = 0x4BA00477


def reset():
    return [(1, 0)] * 5 + [(0, 0)]                       # Test-Logic-Reset, then Run-Test/Idle


def scan(value, n, ir=False):
    """(TMS, TDI) cycles from Run-Test/Idle through Shift-IR / DR (n bits) back to it."""
    head = [(1, 0), (1, 0), (0, 0), (0, 0)] if ir else [(1, 0), (0, 0), (0, 0)]
    bits = [(int(i == n - 1), value >> i & 1) for i in range(n)]
    return head + bits + [(1, 0), (0, 0)]


def run(cycles):
    """Run the cycles; returns TDO per cycle."""
    cycles = cycles + [(0, 0)] * (-len(cycles) % 8)      # pad in Run-Test/Idle
    bench = Bench()
    bench.load(0, (HERE / "jtag.asm").read_text(), out_base=TDI, sideset_base=TCK, in_base=TDO)
    bench.chip.reg_write(0, PIN_MASK, 1 << TDI | 1 << TMS | 1 << TCK)
    tap = bench.add(JtagTap(TCK, TMS, TDI, TDO, IDCODE))
    bench.send(0, [sum((tms << 1 | tdi) << 2 * k for k, (tms, tdi) in enumerate(cycles[i:i + 8]))
                   for i in range(0, len(cycles), 8)])
    bench.start(1)
    bench.run(len(cycles) * 4 + 50)
    tdo = [w >> (8 + k) & 1 for w in bench.received[0] for k in range(8)]
    return bench, tap, tdo


def bits_at(tdo, start, n):
    return sum(b << i for i, b in enumerate(tdo[start:start + n]))


class Jtag(unittest.TestCase):

    def test_idcode_after_reset(self):
        cycles = reset() + scan(0, 32)
        bench, tap, tdo = run(cycles)
        self.assertEqual(bench.errors(), [])
        self.assertEqual(bits_at(tdo, len(reset()) + 3, 32), IDCODE)

    def test_user_register_write_and_read(self):
        pre = reset() + scan(JtagTap.IR_USER, 4, ir=True) + scan(0xBEEF, 16)
        bench, tap, tdo = run(pre + scan(0x1234, 16))
        self.assertEqual(bench.errors(), [])
        self.assertEqual((tap.ir, tap.user), (JtagTap.IR_USER, 0x1234))
        self.assertEqual(bits_at(tdo, len(pre) + 3, 16), 0xBEEF, "the previous value shifts out")

    def test_bypass_is_one_bit(self):
        pre = reset() + scan(JtagTap.IR_BYPASS, 4, ir=True)
        bench, tap, tdo = run(pre + scan(0b1011001, 8))
        self.assertEqual(bits_at(tdo, len(pre) + 3, 8), 0b1011001 << 1 & 0xFF)


if __name__ == "__main__":
    unittest.main()
