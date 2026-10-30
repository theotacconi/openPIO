#!/usr/bin/env python3
"""ISA v2 and PAU v2 checks of the golden model (sim/model/emu.py) against references that do
not come from the model itself: published CRC check values, waveforms written from the line-code
definitions, and a transmitter looped back into a receiver.

    python3 test/model/test_isa_v2.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sim" / "model"))
from asm import assemble          # noqa: E402
from emu import StateMachine      # noqa: E402

CHECK = b"123456789"


def rev(v: int, n: int) -> int:
    return int(f"{v:0{n}b}"[::-1], 2)


def run(sm: StateMachine, words: list[int], cycles: int, record=None) -> list[int]:
    """Step `cycles` clocks, keeping the TX FIFO fed; returns the pins of every clock."""
    words, line = list(words), []
    for _ in range(cycles):
        if words and len(sm.tx_fifo) < sm.fifo_depth:
            sm.tx_fifo.append(words.pop(0))
        sm.step()
        line.append(sm.pins)
    return line


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
        """Nobody reads the 32-bit queue: the 33rd bit time sets the sticky overrun."""
        src = """
            .pau code=nrz baud=2
                set pau_ctl, rx
            end:
                jmp end
        """
        rx = self.loop(src, [0xAAAA, 0xAAAA], 120)
        self.assertEqual((len(rx.rx_queue) < 32, rx._pau_status(7)), (True, 0))
        rx = self.loop(src, [0xAAAA, 0xAAAA], 300)
        self.assertEqual((len(rx.rx_queue), rx._pau_status(7)), (32, 1))

    def test_tx_abort(self):
        sm = StateMachine(assemble("""
            .pau code=nrz baud=4
                set pins, 1
                set pindirs, 1
                set pau_ctl, tx
                out pins, 16          ; 0x0000: the line goes low
                out pins, 16
                nop [7]
                pau tx_abort
            end:
                jmp end
        """))
        sm.tx_fifo.extend([0, 0])
        for _ in range(8):
            sm.step()
        self.assertEqual((sm.pins & 1, len(sm.tx_queue) > 16), (0, True), "sending")
        for _ in range(9):
            sm.step()
        self.assertEqual((sm.pins & 1, sm.tx_queue, sm.pau_ctl & 1, sm._pau_status(0)),
                         (1, type(sm.tx_queue)(), 0, 1), "queue gone, TX off, pin back to its latch")


class Isa(unittest.TestCase):
    """Non-PAU ISA v2 features."""

    def test_alu_operations(self):
        sm = StateMachine(assemble("""
            set x, 12
            set y, 10
            and x, y        ; 8
            or y, x         ; 10
            sub y, x        ; 2
            not x           ; ~8
            mov isr, x
            add x, y        ; ~8 + 2
        end:
            jmp end
        """))
        for _ in range(12):
            sm.step()
        self.assertEqual((sm.y, sm.isr, sm.x), (2, 0xFFF7, 0xFFF9))

    def test_jump_above_32_and_wait_on_x_bit(self):
        src = "jmp far\n" + "nop\n" * 40 + """
        far:
            set x, 4
            wait 1 x 2       ; bit 2 of X
            jmp !x, bad
            jmp x--, done
        bad:
            irq 1
        done:
            irq 0
        end:
            jmp end
        """
        sm = StateMachine(assemble(src))
        for _ in range(10):
            sm.step()
        self.assertTrue(sm.irq_flags[0] and not sm.irq_flags[1])
        self.assertGreaterEqual(sm.pc, 32)

    def test_program_writes_a_condition(self):
        sm = StateMachine(assemble("""
            pull block
            mov cond, osr        ; COND3 = gpio 5
            jmp cond3, high
            irq 1
        high:
            irq 0
        end:
            jmp end
        """))
        sm.gpio_in = 1 << 5
        run(sm, [0x0305], 10)
        self.assertTrue(sm.irq_flags[0] and not sm.irq_flags[1])

    def test_autopush_bytes(self):
        sm = StateMachine(assemble("""
            .push_thresh 8
        loop:
            in pins, 4 auto
            jmp loop
        """))
        sm.gpio_in = 0x5
        for _ in range(12):
            sm.step()
        self.assertEqual([w >> 8 for w in sm.rx_fifo], [0x55, 0x55, 0x55])

    def test_alu_pins(self):
        sm = StateMachine(assemble("""
            set x, 0x15
            mov pindirs, x
            mov pins, x
            mov y, pins
        end:
            jmp end
        """))
        sm.gpio_in = 0xA0
        for _ in range(5):
            sm.step()
        self.assertEqual((sm.pindirs, sm.gpio_out, sm.y), (0x15, 0x15, 0xA0))


if __name__ == "__main__":
    unittest.main()
