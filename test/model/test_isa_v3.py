#!/usr/bin/env python3
"""ISA v3 checks of the golden model (sim/model/emu.py): the line code table against waveforms
written from each code's definition, a transmitter looped back into a receiver for every named
code, the sync hunt, the bit clock (own, on PIN2, and external, in the four SPI modes) and the
line states. References come from the definitions, not from the model.

    python3 test/model/test_isa_v3.py
"""
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sim" / "model"))
from asm import assemble          # noqa: E402
from emu import StateMachine      # noqa: E402


def halves(code: str, bits: list[int], level: int) -> list[int]:
    """The line in each half-bit, from the textbook definition of `code`, from idle `level`."""
    out, p = [], 0
    for b in bits:
        if code == "fm0":                       # biphase space: a toggle at every boundary, mid when 0
            first = level ^ 1
            second = first ^ (b == 0)
        elif code == "diff_manchester":         # a toggle mid-bit always, at the boundary when 0
            first = level ^ (b == 0)
            second = first ^ 1
        elif code == "miller":                  # MFM: mid-bit toggle for a 1, boundary between 0s
            first = level ^ (b == 0 and p == 0)
            second = first ^ (b == 1)
        out += [first, second]
        level, p = second, b
    return out


class LineCodes(unittest.TestCase):
    """PAU_CODE rows against the definitions of FM0, differential Manchester and Miller."""

    def test_waveforms(self):
        rng = random.Random(5)
        for code in ("fm0", "diff_manchester", "miller"):
            bits = [rng.randrange(2) for _ in range(12)]
            h = 4
            sm = StateMachine(assemble(f"""
                .pau code={code} baud={h}
                    set pins, 0
                    set pindirs, 1
                    set pau_ctl, tx
                    pull block
                    out pins, {len(bits)}
                end:
                    jmp end
            """))
            sm.tx_fifo.append(sum(b << i for i, b in enumerate(bits)))
            line = []
            for _ in range(2 * h * len(bits) + 40):
                sm.step()
                line.append(sm.pins & 1)
            # the first bit starts a few strobes in; sample each half in its middle
            got = None
            want = halves(code, bits, 0)
            for start in range(2, 12):          # the strobe the first half starts on
                seen = [line[start + k * h + h // 2] for k in range(2 * len(bits))]
                if seen == want:
                    got = seen
                    break
            self.assertEqual(got, want, f"{code}: {bits}")


class Loopback(unittest.TestCase):
    """Every named code through a transmitter into a receiver: bytes and CRC come back."""

    TX = """
        .pau code={code} baud=3
        .crc width=16 poly=0x1021 init=ones
        .pull_thresh 8
            set pins, 0
            set pindirs, 1
            set pau_ctl, tx
            set x, {pre}            ; preamble bytes, unchecked
        pre:
            pull block
            out pins, 8
            jmp x--, pre
            pau crc_reset
            set pau_ctl, tx|crc
            out pins, 8 auto
            out pins, 8 auto
            out pins, 8 auto
            set pau_ctl, tx
            pau crc_append
            set pau_ctl, 0
        end:
            jmp end
    """
    RX = """
        .pau code={code} baud=3
        {sync}
        .crc width=16 poly=0x1021 init=ones
        .push_thresh 8
            set pau_ctl, rx
{skip}            pau crc_reset
            set pau_ctl, rx|crc
            set x, 4
        byte:
            in pins, 8 auto         ; 3 data bytes and the 2 CRC bytes
            jmp x--, byte
            jmp pau crc_ok, good
            irq 1
        good:
            irq 0
        end:
            jmp end
    """

    def check(self, code: str, preamble: list[int], sync: str = ""):
        tx = StateMachine(assemble(self.TX.format(code=code, pre=len(preamble) - 1)))
        skip = "" if sync else "            in pins, 8               ; the preamble\n" * len(preamble)
        rx = StateMachine(assemble(self.RX.format(code=code, sync=sync, skip=skip)))
        data = [0xA7, 0x00, 0xFF]
        words, got = [*preamble, *data], []
        for _ in range(2000):
            if words and len(tx.tx_fifo) < tx.fifo_depth:
                tx.tx_fifo.append(words.pop(0))
            tx.step()
            rx.gpio_in = tx.pins & 1
            rx.step()
            while rx.rx_fifo:
                got.append(rx.rx_fifo.popleft() >> 8)
        self.assertEqual(got[:3], data, f"{code}: bytes")
        self.assertTrue(rx.irq_flags[0] and not rx.irq_flags[1], f"{code}: CRC")

    def test_fm0(self):
        self.check("fm0", [0x01])               # an edge at every boundary: aligned from the first

    def test_diff_manchester(self):
        self.check("diff_manchester", [0x01])   # a 1 first: the first edge is mid-bit

    def test_miller_with_a_sync_word(self):
        """Miller has no edge it always makes: a preamble of 0s, then the sync byte 0x5A
        (sent LSB first), found by the hunt; the receiver passes on the bits after it."""
        self.check("miller", [0x00, 0x5A], ".sync word=0x5A len=8")


class SyncHunt(unittest.TestCase):

    def test_bits_pass_on_after_the_sync_word(self):
        rx = StateMachine(assemble("""
            .pau code=nrz baud=2
            .sync word=0b1011 len=4
            .push_thresh 8
                set pau_ctl, rx
            byte:
                in pins, 8 auto
                jmp byte
        """))
        # noise, the sync word 1011 in time order from bit 0 (1, 1, 0, 1), then 0xE5 LSB first
        stream = [1, 1, 1, 0, 1, 1, 0, 0, 1] + [1, 1, 0, 1] + [1, 0, 1, 0, 0, 1, 1, 1] + [0] * 4
        levels = [b for b in stream for _ in range(4)]          # a bit = 2H = 4 strobes
        for lv in levels:
            rx.gpio_in = lv
            rx.step()
        self.assertTrue(rx._pau_status(5), "sync seen")
        self.assertEqual([w >> 8 for w in rx.rx_fifo][:1], [0b11100101], "the byte after it")


class BitClock(unittest.TestCase):
    """A PAU transmitter with the bit clock on PIN2 into a PAU receiver on that external
    clock, in the four SPI modes; the receiver answers on its own pin, clocked the same way."""

    TX = """
        .out_pins 0
        .pau code=nrz baud=3 pin2=clock cpol={cpol} cpha={cpha}
        .shift_out left
            set pins, {idle}            ; the clock idles at CPOL before the PAU drives it
            set pindirs, 3
            set pau_ctl, tx
            pull block
            out pins, 16
        end:
            jmp end
    """
    RX = """
        .in_pins 0                  ; data 0, clock 1
        .out_pins 4
        .pau code=nrz ext cpol={cpol} cpha={cpha}
        .shift_in left
        .shift_out left
        .push_thresh 16
            set pindirs, 1
            pull block              ; the answer, queued before the first clock edge
            set pau_ctl, tx|rx
            out pins, 16
            in pins, 16 auto
        end:
            jmp end
    """

    def test_spi_modes(self):
        for mode in range(4):
            cpol, cpha = mode >> 1, mode & 1
            with self.subTest(mode=mode):
                tx = StateMachine(assemble(self.TX.format(cpol=cpol, cpha=cpha, idle=cpol << 1)))
                rx = StateMachine(assemble(self.RX.format(cpol=cpol, cpha=cpha)))
                tx.bus.out = cpol << 1          # a master's clock idles at CPOL from the start
                rx.gpio_in = cpol << 1
                rx.reset()
                tx.tx_fifo.append(0xC35A)
                rx.tx_fifo.append(0x9E17)
                clk_prev, sampled, idle = None, [], []
                for t in range(400):
                    rx.step()                   # the answer is queued first
                    if t > 6:
                        tx.step()
                    clk = tx.pins >> 1 & 1
                    if t <= 6:
                        idle.append(clk)
                    if clk_prev is not None and clk != clk_prev and clk == (cpol == cpha):
                        sampled.append(rx.pins >> 4 & 1)    # a master samples the answer here
                    clk_prev = clk
                    rx.gpio_in = tx.pins & 3
                self.assertEqual([w for w in rx.rx_fifo], [0xC35A], "data in on the clock")
                self.assertEqual(sum(b << (15 - i) for i, b in enumerate(sampled[:16])), 0x9E17,
                                 "the answer out on the clock")
                self.assertEqual(tx.pins >> 1 & 1, cpol, "the clock idles at CPOL")


class LineStates(unittest.TestCase):

    def test_four_values_of_two_pins(self):
        sm = StateMachine(assemble("jmp 0\n"), in_base=6)
        for v in range(4):
            sm.gpio_in = (v & 1) << 6 | (v >> 1) << 7
            self.assertEqual([sm._pau_status(8 + k) for k in range(4)],
                             [int(k == v) for k in range(4)], f"in_base + 1, in_base = {v:02b}")

    def test_complement_on_pin2(self):
        sm = StateMachine(assemble("""
            .out_pins 2
            .pau code=nrz baud=2 pin2=complement
                set pau_ctl, tx
                pull block
                out pins, 8
            end:
                jmp end
        """))
        sm.tx_fifo.append(0b10110010)
        for _ in range(40):
            sm.step()
            if sm.tx_drives:
                self.assertEqual(sm.pins >> 3 & 1, 1 - (sm.pins >> 2 & 1))


if __name__ == "__main__":
    unittest.main()
