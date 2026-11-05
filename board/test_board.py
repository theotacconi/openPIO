#!/usr/bin/env python3
"""The board's tools on the Python model, through the bridge's firmware: the bring-up checks,
and listen.py from a recording to the frames printed, in each family of the learner. The
recordings are sigrok session files of the reference devices and the chip's programs, played
on the model's pins as a logic analyzer would have seen them.

    python3 board/test_board.py
"""
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "test" / "learn")]
import bringup                                           # noqa: E402
import loopback                                          # noqa: E402
from board import Board, Model                           # noqa: E402
from host import Host                                    # noqa: E402
from learn import write_sigrok                           # noqa: E402
import test_learn as T                                   # noqa: E402


class BringUp(unittest.TestCase):

    def test_checks_pass_on_the_model(self):
        board = Board(Model())
        host = Host(board)
        for check in bringup.CHECKS:
            host.write(0, 0x20, 0x30)
            self.assertIsNone(check(board, host), check.__name__)


class Loopback(unittest.TestCase):

    def test_checks_pass_on_the_model(self):
        host = Host(Board(Model()))
        for check in loopback.CHECKS:
            loopback.release(host)
            self.assertIsNone(check(host), check.__name__)


class Listen(unittest.TestCase):
    """listen.py --model on a recording at 1 sample a clock."""

    def listen(self, levels: list[int], *args: str) -> list[str]:
        with tempfile.TemporaryDirectory() as d:
            write_sigrok(Path(d) / "rec.sr", levels, 1e6)
            out = subprocess.run([sys.executable, str(HERE / "listen.py"), str(Path(d) / "rec.sr"),
                                  "--model", "--clock-hz", "1e6", *args],
                                 capture_output=True, text=True, check=True).stdout
        return out.splitlines()

    def test_hdlc_frames_with_their_crc(self):
        """One frame of the recording corrupted on the line: the CRC is learned from the
        others, and that frame is printed with the CRC wrong."""
        rnd = random.Random(1)
        frames = [[rnd.randrange(256) for _ in range(n)] for n in (4, 9, 2, 12, 6, 3, 7, 5)]
        levels = T.hdlc_trace(frames)
        edges = T.bursts(T.Capture.from_levels(levels), 80)[3][1]
        t = next(t for t in range(edges[0] + 8 * 50, edges[-1], 8) if t not in edges)
        levels[t:] = [lv ^ 1 for lv in levels[t:]]      # NRZI: a bit of the fourth frame flipped
        out = self.listen(levels, "--lsb")
        self.assertIn("learned frames: nrzi", out[0])
        self.assertIn("(one recorded frame wrong)", out[0])
        got = [line.split() for line in out[3:]]
        self.assertEqual(len(got), len(frames))
        for k, (words, f) in enumerate(zip(got, frames)):
            self.assertEqual(words[-2:], ["CRC", "WRONG" if k == 3 else "ok"])
            if k != 3:
                self.assertEqual(words[2:3 + len(f)], [f"{b:02x}" for b in [0x7E, *f]])

    def test_nec_remote(self):
        rnd = random.Random(2)
        frames = [(rnd.randrange(256), rnd.randrange(256)) for _ in range(4)]
        bench = T.Bench()
        remote = bench.add(T.NecRemote(0, frames, us=0.2, start=100, gap=40000))
        bench.run(remote.end + 100)
        out = self.listen(bench.trace, "--lsb")
        self.assertIn("learned pulses: pulse distance", out[0])
        self.assertEqual(out[3:], [f"  32 bits  {a:02x} {a ^ 255:02x} {c:02x} {c ^ 255:02x}"
                                   for a, c in frames])

    def test_uart(self):
        text = b"openPIO, from a recording"
        bench = T.Bench(pullups=1)
        tx = bench.add(T.UartTx(0, 40, list(text), start=300))
        bench.run(tx.end + 300)
        out = self.listen(bench.trace)
        self.assertIn("learned async: async serial 8N1", out[0])
        self.assertEqual("".join(eval(line.split(maxsplit=1)[1]) for line in out[3:]),
                         text.decode())

    def test_its_own_line(self):
        """A transmitter on SM 1 keeps running under listen.py: the learned receiver on SM 0
        reads it from the same pad, as on the board, where it is loaded before listen.py."""
        import contextlib
        import io
        import listen
        from asm import assemble
        text = b"openPIO learns its own line"
        bench = T.Bench(pullups=1)
        tx = bench.add(T.UartTx(0, 40, list(b"a recording of the chip's UART"), start=300))
        bench.run(tx.end + 300)

        class Model(listen.Model):                       # SM 1: uart_tx, 40 clocks a bit
            def __init__(self, play=None):
                super().__init__(play)
                Host(self.chip).load(1, assemble((HERE.parent / "programs/uart/uart_tx.asm")
                                                 .read_text()), clkdiv_int=5)
                Host(self.chip).start(0b10)
                self.sent = False

            def ask(self, line):
                if self.chip.t0 is not None and not self.sent:   # once SM 0 runs
                    self.sent = True
                    Host(self.chip).push(1, list(text))
                return super().ask(line)

        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(out):
            write_sigrok(Path(d) / "rec.sr", bench.trace, 1e6)
            argv, sys.argv = sys.argv, ["listen.py", str(Path(d) / "rec.sr"), "--model",
                                        "--clock-hz", "1e6"]
            model, listen.Model = listen.Model, Model
            try:
                listen.main()
            finally:
                sys.argv, listen.Model = argv, model
        got = out.getvalue().splitlines()[3:]
        self.assertEqual("".join(eval(line.split(maxsplit=1)[1]) for line in got),
                         text.decode())

    def test_ps2_keyboard_with_its_parity(self):
        rnd = random.Random(3)
        keys = [rnd.randrange(256) for _ in range(44)]
        bench = T.Bench(pullups=3)
        bench.add(T.Ps2Device(1, 0, keys))
        bench.run(50 + 1080 * len(keys) + 300)
        out = self.listen(bench.trace, "--channel", "D0", "--clock-channel", "D1", "--pin", "2")
        self.assertIn("learned clocked: data pin D0 on clock pin D1, mode 2", out[0])
        want = [f"  11 bits  {int(''.join(map(str, T.ps2_bits(k)[:8])), 2):02x} +" for k in keys]
        got = out[3:]
        self.assertEqual(len(got), len(keys))
        for line, w in zip(got, want):
            self.assertTrue(line.startswith(w) and line.endswith("CRC ok"), line)


if __name__ == "__main__":
    unittest.main()
