#!/usr/bin/env python3
"""LIN master against a reference LIN slave on a wired-AND bus: break of 13 bits, sync 0x55,
protected ID parity, a response from the slave (data and checksum read back) and one from the
master (checksum checked by the slave); random settings inside what the master promises (a
divider, a slave 2 % fast or slow answering at once or later, bytes back to back or apart).
sigrok's LIN decoder reads the same break, sync, IDs with their parity, data and checksums on
the bus."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "test" / "shared"))
from bench import Bench                                        # noqa: E402
from devices import LinSlave, WiredAndBus, lin_checksum, lin_pid  # noqa: E402
from sigrok import lin as sigrok_lin                           # noqa: E402
from seeds import seeded                                       # noqa: E402

TX, RX, BIT = 0, 1, 8  # pins as in the program


class LinMaster(unittest.TestCase):

    def test_slave_and_master_responses(self):
        slave = LinSlave(BIT, publish={0x10: [0x11, 0x22, 0x33]}, subscribe={0x21: 2})
        bench = Bench(pullups=1 << TX | 1 << RX)
        bench.load(0, (HERE / "lin_master.asm").read_text())
        bench.add(WiredAndBus([TX], [RX], [slave]))
        pub, data = lin_pid(0x21), [0xA5, 0x5A]
        bench.send(0, [lin_pid(0x10) | 4 << 8,                                  # slave answers
                       pub | 3 << 8 | 1 << 15] + data + [lin_checksum(pub, data)])  # master sends
        bench.start(1)
        bench.run(2500)
        self.assertEqual(bench.errors(), [])
        got = [w >> 8 for w in bench.received[0]]
        self.assertEqual(got, [0x11, 0x22, 0x33, lin_checksum(lin_pid(0x10), [0x11, 0x22, 0x33])])
        self.assertEqual(slave.frames, [(0x10, [0x11, 0x22, 0x33], True), (0x21, data, True)])

        def frame(ident, data):
            return (["Break condition", "Sync", f"ID: {ident:02X} Parity: {lin_pid(ident) >> 6} (ok)"]
                    + [f"Data: 0x{d:02X}" for d in data]
                    + [f"Checksum: 0x{lin_checksum(lin_pid(ident), data):02X}"])
        self.assertEqual(sigrok_lin(bench, RX, BIT), frame(0x10, [0x11, 0x22, 0x33]) + frame(0x21, data),
                         "sigrok")

    def test_sweep(self):
        """Random settings inside what lin_master.asm promises: any divider (fractional from
        2), a slave whose bits are up to 2 % short or long after it synchronized (LIN's
        tolerance then), answering at once after the header or up to 2 bits later, its bytes
        back to back or up to 2 bits apart (both spaces may be 0), 1 to 8 data bytes rich in
        0x00 and 0xFF; then a response from the master. Every byte read, every checksum right."""
        rnd = seeded(21)
        for trial in range(4):
            clkdiv = rnd.choice([1, 2, rnd.randrange(3, 12)])
            frac = rnd.choice([0, rnd.randrange(256)]) if clkdiv > 1 else 0
            off = [-0.02, 0.02, rnd.uniform(-0.02, 0.02), rnd.uniform(-0.02, 0.02)][trial]
            space, spaces = [(0, 0), (0, 0), (rnd.uniform(0, 2), 0), (rnd.uniform(0, 2), 2)][trial]
            bit = BIT * (clkdiv + frac / 256)
            pub, sub = rnd.sample(range(0x3C), 2)
            data = [rnd.choice([0x00, 0xFF, rnd.randrange(256)]) for _ in range(rnd.randrange(1, 9))]
            mine = [rnd.randrange(256) for _ in range(rnd.randrange(1, 9))]
            with self.subTest(trial=trial, clkdiv=clkdiv, frac=frac, off=f"{off:+.2%}", space=f"{space:.2f}"):
                slave = LinSlave(round(bit), publish={pub: data}, subscribe={sub: len(mine)}, space=space,
                                 spaces=lambda: rnd.uniform(0, spaces), send_bit=bit * (1 + off))
                bench = Bench(pullups=1 << TX | 1 << RX)
                bench.load(0, (HERE / "lin_master.asm").read_text(), clkdiv_int=clkdiv, clkdiv_frac=frac)
                bench.add(WiredAndBus([TX], [RX], [slave]))
                p = lin_pid(sub)
                bench.send(0, [lin_pid(pub) | (len(data) + 1) << 8,
                               p | (len(mine) + 1) << 8 | 1 << 15] + mine + [lin_checksum(p, mine)])
                bench.start(1)
                bench.run(int(bit * (34 + 30 * 10 * 3)))
                self.assertEqual(bench.errors(), [])
                self.assertEqual([w >> 8 for w in bench.received[0]],
                                 data + [lin_checksum(lin_pid(pub), data)])
                self.assertEqual(slave.frames, [(pub, data, True), (sub, mine, True)])


if __name__ == "__main__":
    unittest.main()
