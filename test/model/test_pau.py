#!/usr/bin/env python3
"""PAU checks of the golden model (sim/model/emu.py) against references that do not come from the
model itself: published CRC check values, waveforms written from each line code's definition, a
transmitter looped back into a receiver for every named code (with stuffing and the CRC residue,
or after a preamble, Miller's found by a sync hunt in software), receive errors and TX abort,
the receiver's half-bit rounding, the bit clock (own, on PIN2, and external, in the four SPI
modes) and the line states.

    python3 test/model/test_pau.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sim" / "model"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "test" / "shared"))
from asm import assemble          # noqa: E402
from emu import StateMachine      # noqa: E402
from seeds import seeded          # noqa: E402

CHECK = b"123456789"


def rev(v: int, n: int) -> int:
    return int(f"{v:0{n}b}"[::-1], 2)


def run(sm: StateMachine, words: list[int], cycles: int) -> list[int]:
    """Step `cycles` clocks, keeping the TX FIFO fed; returns the pins of every clock."""
    words, line = list(words), []
    for _ in range(cycles):
        if words and len(sm.tx_fifo) < sm.fifo_depth:
            sm.tx_fifo.append(words.pop(0))
        sm.step()
        line.append(sm.pins)
    return line


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


class Crc(unittest.TestCase):
    """CRC engine against the catalogue check values of '123456789' (reveng.sourceforge.io)."""

    def crc_of(self, data: bytes, cfg: str, lsb_first: bool) -> StateMachine:
        sm = StateMachine(assemble(f"""
            .pau code=nrz baud=1
            .crc {cfg}
            .pull_thresh 8
                pau crc_reset
                set pau_ctl, tx|crc
            loop:
                out pins, 8 auto
                jmp loop
        """))
        sm.shift_out_right = lsb_first
        run(sm, [b if lsb_first else b << 8 for b in data], 400)
        return sm

    @staticmethod
    def value(sm: StateMachine, width: int) -> int:
        """The CRC register is MSB-aligned: the CRC is its top `width` bits."""
        return sm.crc >> (32 - width)

    def test_usb_crc16(self):        # CRC-16/USB: refin/refout, init ones, xorout ones
        sm = self.crc_of(CHECK, "width=16 poly=0x8005 init=ones xorout=1", True)
        self.assertEqual(rev(self.value(sm, 16) ^ 0xFFFF, 16), 0xB4C8)

    def test_ethernet_crc32(self):   # CRC-32 (IEEE 802.3)
        sm = self.crc_of(CHECK, "width=32 poly=0x04C11DB7 init=ones xorout=1", True)
        self.assertEqual(rev(self.value(sm, 32) ^ 0xFFFFFFFF, 32), 0xCBF43926)

    def test_can_crc15(self):        # CRC-15/CAN: MSB first, init 0
        sm = self.crc_of(CHECK, "width=15 poly=0x4599", False)
        self.assertEqual(self.value(sm, 15), 0x059E)

    def test_onewire_crc8(self):     # CRC-8/MAXIM-DOW: reflected, init 0
        sm = self.crc_of(CHECK, "width=8 poly=0x31", True)
        self.assertEqual(rev(self.value(sm, 8), 8), 0xA1)

    def test_parity(self):           # a 1-bit CRC with polynomial 1 is the even parity
        sm = self.crc_of(b"\x07", "width=1 poly=1", True)
        self.assertEqual(self.value(sm, 1), 1)


class LineCodes(unittest.TestCase):
    """TX waveforms from the line-code definitions (ISA.md, Transmitter), H = 2 strobes."""

    BITS = [1, 0, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]

    def expected(self, enc: str) -> str:
        level, out = 0, []
        for b in self.BITS:
            if enc == "nrz":
                halves = [b, b]
            elif enc == "nrzi":
                level ^= 1 - b
                halves = [level, level]
            elif enc == "manchester":
                halves = [1 - b, b]
            else:
                level ^= 1
                first = level
                level ^= b
                halves = [first, level]
            out += [halves[0]] * 2 + [halves[1]] * 2
        return "".join(map(str, out))

    def test_waveforms(self):
        word = sum(b << i for i, b in enumerate(self.BITS))
        for enc in ("nrz", "nrzi", "manchester", "bmc"):
            with self.subTest(enc=enc):
                sm = StateMachine(assemble(f"""
                    .pau code={enc} baud=2
                        set pau_ctl, tx
                        pull block
                        out pins, 12
                    end:
                        jmp end
                """))
                line = "".join(str(p & 1) for p in run(sm, [word], 80))
                self.assertIn(self.expected(enc), line)

    def test_usb_stuffing_takes_a_bit_time(self):
        sm = StateMachine(assemble("""
            .pau code=nrz baud=1 stuff=6
                set pau_ctl, tx|stuff
                pull block
                out pins, 9
            end:
                jmp end
        """))
        line = "".join(str(p & 1) for p in run(sm, [0b011111111], 60))
        self.assertIn("11" * 6 + "00" + "11" * 2 + "00", line)   # six 1s, stuffed 0, then 1, 1, 0

    def test_can_stuffing_equal_bits(self):
        sm = StateMachine(assemble("""
            .pau code=nrz baud=1 stuff=5 stuff_mode=equal
                set pins, 1
                set pau_ctl, tx|stuff
                pull block
                out pins, 11
            end:
                jmp end
        """))
        # 00000 -> stuffed 1, then 11111 (the stuffed 1 counts: 1 + four 1s) -> stuffed 0, then 1
        line = "".join(str(p & 1) for p in run(sm, [0b11111100000], 60))
        self.assertIn("00" * 5 + "11" + "11" * 4 + "00" + "11" * 2, line)


class TransitionCodes(unittest.TestCase):
    """PAU_CODE rows against the definitions of FM0, differential Manchester and Miller."""

    def test_waveforms(self):
        rnd = seeded(5)
        for code in ("fm0", "diff_manchester", "miller"):
            bits = [rnd.randrange(2) for _ in range(12)]
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
    """A PAU transmitter wired into a PAU receiver: bytes, stuffing and CRC come back."""

    TX = """
        .pau code={enc} baud=3 {stuff}
        .crc width=16 poly=0x8005 init=ones xorout=1 res=0x800D
        .pull_thresh 8
            set pins, {idle}
            set pindirs, 1
            pau crc_reset
            set pau_ctl, tx|crc{tx_stuff}
            out pins, 8 auto
            out pins, 8 auto
            out pins, 8 auto
            out pins, 8 auto
            set pau_ctl, tx{tx_stuff}
            pau crc_append
        end:
            jmp end
    """
    RX = """
        .pau code={enc} baud=3 {stuff}
        .crc width=16 poly=0x8005 init=ones xorout=1 res=0x800D
        .push_thresh 8
            pau crc_reset
            set pau_ctl, rx|crc{rx_stuff}
            set x, 5
        byte:
            in pins, 8 auto           ; 4 data bytes and the 2 CRC bytes
            jmp x--, byte
            jmp pau crc_ok, good      ; checked right after the last bit
            irq 1
        good:
            irq 0
            set pau_ctl, 0
        end:
            jmp end
    """

    def check(self, enc: str, stuff: str, first: int):
        cfg = dict(enc=enc, stuff=stuff, tx_stuff="|stuff" if stuff else "",
                   rx_stuff="|stuff" if stuff else "", idle=1 - first if enc in ("nrz", "nrzi") else 0)
        tx = StateMachine(assemble(self.TX.format(**cfg)))
        rx = StateMachine(assemble(self.RX.format(**cfg)))
        data = [first | 0xF0, 0xFF, 0xFF, 0x3C]
        words, got = list(data), []
        for _ in range(900):
            if words and len(tx.tx_fifo) < tx.fifo_depth:
                tx.tx_fifo.append(words.pop(0))
            tx.step()
            rx.gpio_in = tx.pins & 1
            rx.step()
            while rx.rx_fifo:                     # the host drains the RX FIFO
                got.append(rx.rx_fifo.popleft() >> 8)
        self.assertEqual(got[:4], data, f"{enc}: bytes")
        self.assertTrue(rx.irq_flags[0] and not rx.irq_flags[1], f"{enc}: CRC == residue after data + CRC")
        self.assertEqual(rx._pau_status(6), 0, f"{enc}: stuff error")

    def test_nrz(self):
        self.check("nrz", "", 0)

    def test_nrzi_usb_stuffing(self):
        self.check("nrzi", "stuff=6", 0)

    def test_manchester(self):
        self.check("manchester", "", 1)

    def test_biphase_mark(self):
        self.check("bmc", "", 1)


class LoopbackAfterAPreamble(unittest.TestCase):
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
        .crc width=16 poly=0x1021 init=ones
        .push_thresh 8
{skip}            mov isr, null
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

    SKIP = """\
            set pau_ctl, rx
{take}            pau crc_reset
            set pau_ctl, rx|crc
"""
    # the sync hunt in software: Y = the sync byte, OSR = its mask; the ISR shifts right, so
    # the last 8 bits decoded are bits 15:8. The CRC, on from the start, is reset after each
    # bit hunted: it holds the bits after the sync word.
    HUNT = """\
            pull block
            mov y, osr
            pull block
            set pau_ctl, rx|crc
        hunt:
            in pins, 1
            pau crc_reset
            mov x, isr
            and x, osr
            jmp x!=y, hunt
"""

    def check(self, code: str, preamble: list[int], sync: int | None = None):
        tx = StateMachine(assemble(self.TX.format(code=code, pre=len(preamble) - 1)))
        skip = (self.HUNT if sync is not None else self.SKIP.format(
            take="            in pins, 8               ; the preamble\n" * len(preamble)))
        rx = StateMachine(assemble(self.RX.format(code=code, skip=skip)))
        if sync is not None:
            rx.tx_fifo.extend([sync << 8, 0xFF00])
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
        (sent LSB first), found by the program's hunt; the CRC starts after it."""
        self.check("miller", [0x00, 0x5A], 0x5A)


class PauErrors(unittest.TestCase):
    """RX errors and TX abort, with a plain NRZ transmitter wired into the receiver."""

    TX = """
        .pau code=nrz baud=2
            set pins, 1
            set pindirs, 1
            set pau_ctl, tx
        loop:
            out pins, 16 auto
            jmp loop
    """

    def loop(self, rx_src: str, words: list[int], clocks: int = 700) -> StateMachine:
        tx, rx = StateMachine(assemble(self.TX)), StateMachine(assemble(rx_src))
        words = list(words)
        for _ in range(clocks):
            if words and len(tx.tx_fifo) < tx.fifo_depth:
                tx.tx_fifo.append(words.pop(0))
            tx.step()
            rx.gpio_in = tx.pins & 1
            rx.step()
        return rx

    def test_stuff_error(self):
        """LSB first: five 1s then the stuffed 0 are right, six 1s are not (bits 1..6 of a word,
        zeros around; checked inside the 32 bits of 4 strobes)."""
        src = """
            .pau code=nrz baud=2 stuff=5
                set pau_ctl, rx|stuff
            end:
                jmp end
        """
        self.assertEqual(self.loop(src, [0x003E, 0], 120)._pau_status(6), 0, "stuffed 0 found")
        self.assertEqual(self.loop(src, [0x007E, 0], 120)._pau_status(6), 1, "sixth 1")

    def test_flag_arrives_whole_and_crc_reset_clears(self):
        """Stuffing 5: an HDLC flag 01111110 (LSB first: 0x7E) sets stuff_err but all 8 bits
        arrive, the wrong bit restarting the count; CRC reset clears the error."""
        rx = self.loop("""
            .pau code=nrz baud=2 stuff=5
            .push_thresh 8
                set pau_ctl, rx|stuff
                in pins, 8 auto      ; 0x00, the first byte
                in pins, 8 auto      ; the flag
                mov x, status
                pau crc_reset
                mov y, status
            end:
                jmp end
        """, [0x7E00, 0x0001])          # a 1 after the flag
        self.assertEqual([w >> 8 for w in rx.rx_fifo], [0x00, 0x7E])
        self.assertEqual((rx.x >> 14 & 1, rx.y >> 14 & 1), (1, 0), "stuff_err, then cleared")

    def test_overrun(self):
        """Nobody reads the 16-bit queue: the 17th bit time sets the sticky overrun."""
        src = """
            .pau code=nrz baud=2
                set pau_ctl, rx
            end:
                jmp end
        """
        rx = self.loop(src, [0xAAAA, 0xAAAA], 50)
        self.assertEqual((len(rx.rx_queue) < 16, rx._pau_status(7)), (True, 0))
        rx = self.loop(src, [0xAAAA, 0xAAAA], 300)
        self.assertEqual((len(rx.rx_queue), rx._pau_status(7)), (16, 1))

    def test_tx_abort(self):
        sm = StateMachine(assemble("""
            .pau code=nrz baud=4
                set pins, 1
                set pindirs, 1
                set pau_ctl, tx
                out pins, 8           ; 0x00: the line goes low
                out pins, 8
                nop [7]
                pau tx_abort
            end:
                jmp end
        """))
        sm.tx_fifo.extend([0, 0])
        for _ in range(8):
            sm.step()
        self.assertEqual((sm.pins & 1, len(sm.tx_queue) > 8), (0, True), "sending")
        for _ in range(9):
            sm.step()
        self.assertEqual((sm.pins & 1, sm.tx_queue, sm.pau_ctl & 1, sm._pau_status(0)),
                         (1, type(sm.tx_queue)(), 0, 1), "queue gone, TX off, pin back to its latch")


class HalfBits(unittest.TestCase):
    """Transition codes: the time between two edges is taken as the nearest whole number of
    half-bits, a tie as the fewer (ISA.md, Receiver): with H = 3, up to 4 strobes is one, 5 to
    7 two. A line from another clock (10BASE-T at 60 MHz) has edges a clock early or late."""

    RX = """
        .pau code=manchester baud=3
        .push_thresh 8
            set pau_ctl, rx
        .wrap_target
            in pins, 1 auto
        .wrap
    """

    def received(self, gaps: list[int]) -> list[int]:
        """The bits decoded from edges `gaps` strobes apart, the first mid-bit."""
        rx = StateMachine(assemble(self.RX))
        level, t, edges = 0, 10, [10]
        for g in gaps:
            t += g
            edges.append(t)
        bits = []
        for k in range(t + 40):
            if k in edges:
                level ^= 1
            rx.gpio_in = level
            rx.step()
            while rx.rx_fifo:
                w = rx.rx_fifo.popleft() >> 8
                bits += [w >> i & 1 for i in range(8)]
        return bits + [rx.isr >> 16 - rx.isr_count + i & 1 for i in range(rx.isr_count)]

    def test_nearest_whole_half_bits(self):
        # Manchester 1 1 0 0 1 0 from an idle low line: edges 3 3 6 3 3 6 6 strobes apart
        self.assertEqual(self.received([3, 3, 6, 3, 3, 6, 6]), [1, 1, 0, 0, 1, 0])
        for early_late in ([4, 2, 5, 4, 2, 7, 5], [2, 4, 7, 2, 4, 5, 7]):
            self.assertEqual(self.received(early_late), [1, 1, 0, 0, 1, 0], early_late)


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
