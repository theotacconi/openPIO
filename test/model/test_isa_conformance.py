#!/usr/bin/env python3
"""ISA conformance of the golden model: every instruction and field of sim/model/ISA.md, with
expected values worked out from the specification, not from emu.py.

    python3 test/model/test_isa_conformance.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sim" / "model"))
from asm import AssemblerError, assemble   # noqa: E402
from emu import StateMachine      # noqa: E402


def sm_of(src: str, **cfg) -> StateMachine:
    sm = StateMachine(assemble(src))
    for k, v in cfg.items():
        setattr(sm, k, v)
    return sm


def run(sm: StateMachine, n: int) -> StateMachine:
    for _ in range(n):
        sm.step()
    return sm


HALT = "\nhalt:\n    jmp halt\n"


class Jmp(unittest.TestCase):

    def test_encoding_of_targets_above_31(self):
        w = assemble("jmp 40\n" + "nop\n" * 40)[0]
        self.assertEqual((w >> 13, w & 0x1F), (1, 8))
        sm = run(StateMachine(assemble("jmp 40\n" + "nop\n" * 40)), 1)
        self.assertEqual(sm.pc, 40)

    def test_pages_and_far(self):
        """JMP stays in its page; FAR goes to an entry (0..15) of any page; code runs on
        across a page end."""
        sm = run(sm_of("jmp far, p1\nnop\n.page 1\np1: jmp 70\nnop\nnop\nnop\nnop\n"
                       "nop\nnop\njmp far, 0"), 1)
        self.assertEqual(sm.pc, 64)
        self.assertEqual(run(sm, 1).pc, 70, "a JMP in page 1 targets page 1")
        self.assertEqual(run(sm, 2).pc, 0, "FAR to entry 0 of page 0")
        word = assemble("jmp far, 193\n")[0]
        self.assertEqual((word >> 13, word >> 5 & 7, word & 0x1F), (1, 1, 0b10001), "page 3, entry 1")
        sm = run(StateMachine(assemble(".page 1\n" + "nop\n" * 64 + "end: jmp end")), 64 + 64)
        self.assertEqual(sm.pc, 128, "page 0's NOPs, page 1, then page 2")
        for bad in ("jmp 64\n", ".page 1\njmp 0\n", "jmp far, 16\n"):
            with self.subTest(bad), self.assertRaises(AssemblerError):
                assemble(bad)

    def test_x_and_y_post_decrement(self):
        for reg in "xy":
            sm = sm_of(f"jmp {reg}--, 2\nnop\nnop" + HALT)
            setattr(sm, reg, 0)
            run(sm, 1)
            self.assertEqual((sm.pc, getattr(sm, reg)), (1, 0xFFFF), f"{reg} = 0: not taken, wraps")
            sm = sm_of(f"jmp {reg}--, 2\nnop\nnop" + HALT)
            setattr(sm, reg, 1)
            run(sm, 1)
            self.assertEqual((sm.pc, getattr(sm, reg)), (2, 0), f"{reg} = 1: taken, then 0")

    def test_programmable_conditions(self):
        sm = sm_of("jmp cond2, 3\njmp cond0, 3\nnop\nnop")
        sm.cond = [0x60, 0x61, 0x85, 0xE4]              # COND2 = NOT gpio 5
        sm.gpio_in = 1 << 5
        self.assertEqual(run(sm, 1).pc, 1, "gpio 5 high: NOT gpio 5 false")
        self.assertEqual(run(sm, 1).pc, 3, "X == 0 true")

    def test_delay_after_a_jump(self):
        sm = sm_of("jmp 2 [3]\nnop\nset x, 7" + HALT)
        run(sm, 4)
        self.assertEqual((sm.pc, sm.x), (2, 0), "1 strobe + 3 delay strobes, then at target")
        self.assertEqual(run(sm, 1).x, 7)


class Wrap(unittest.TestCase):

    def test_wrap_bottom_top(self):
        sm = sm_of("set x, 1\n.wrap_target\nset y, 2\nset y, 3\n.wrap\nset x, 9")
        pcs = [run(sm, 1).pc for _ in range(5)]
        self.assertEqual(pcs, [1, 2, 1, 2, 1])
        self.assertEqual(sm.x, 1, "instruction after wrap_top never runs")


class In(unittest.TestCase):

    def test_shift_right_and_left(self):
        sm = sm_of("in x, 4\nin y, 4" + HALT)
        sm.x, sm.y = 0xA, 0x5
        run(sm, 2)
        self.assertEqual((sm.isr, sm.isr_count), (0x5A00, 8))
        sm = sm_of("in x, 4\nin y, 4" + HALT, shift_in_right=False)
        sm.x, sm.y = 0xA, 0x5
        run(sm, 2)
        self.assertEqual((sm.isr, sm.isr_count), (0x00A5, 8))

    def test_count_saturates_at_16(self):
        sm = run(sm_of("in null, 12\nin null, 12" + HALT), 2)
        self.assertEqual(sm.isr_count, 16)

    def test_pins_from_in_base_and_status(self):
        sm = sm_of("in pins, 3\nin status, 16" + HALT, in_base=2, shift_in_right=False)
        sm.gpio_in = 0b10110100
        run(sm, 1)
        self.assertEqual(sm.isr, 0b101)
        status = sm.status_word()
        run(sm, 1)
        self.assertEqual(sm.isr, status)

    def test_reserved_pindirs_source_reads_zero(self):
        sm = sm_of(".word 0x4028" + HALT, shift_in_right=False)   # in pindirs, 8
        sm.pindirs = 0xFF
        sm.isr = 0xFFFF
        run(sm, 1)
        self.assertEqual(sm.isr, 0xFF00)

    def test_autopush_stalls_on_full_rx_fifo(self):
        sm = sm_of(".push_thresh 4\nin x, 4 auto\nset y, 1" + HALT)
        sm.x = 0xC
        sm.rx_fifo.extend([1, 2, 3, 4])
        run(sm, 3)
        self.assertEqual((sm.pc, sm.isr_count, sm.y), (0, 4, 0), "shifted, waiting to push")
        sm.rx_fifo.popleft()
        run(sm, 1)
        self.assertEqual((sm.rx_fifo[-1], sm.isr_count), (0xC000, 0))
        self.assertEqual(run(sm, 1).y, 1)


class Out(unittest.TestCase):

    def test_shift_right_and_left(self):
        sm = sm_of("out x, 4" + HALT)
        sm.osr, sm.osr_count = 0x1234, 0
        run(sm, 1)
        self.assertEqual((sm.x, sm.osr, sm.osr_count), (0x4, 0x0123, 4))
        sm = sm_of("out x, 4" + HALT, shift_out_right=False)
        sm.osr, sm.osr_count = 0x1234, 0
        run(sm, 1)
        self.assertEqual((sm.x, sm.osr, sm.osr_count), (0x1, 0x2340, 4))

    def test_destinations(self):
        sm = sm_of("out pins, 3\nout pindirs, 2\nout isr, 5\nout null, 2\nout pc, 6\nnop\nnop\nset x, 1" + HALT,
                   out_base=14)
        sm.osr, sm.osr_count = 0b000111_10_10101_01_111, 0      # pc 7, null, isr 0x15, dirs 01, pins 111
        run(sm, 6)
        self.assertEqual(sm.gpio_out, 0b11 << 14, "pins 14, 15; pin 16 does not exist")
        self.assertEqual(sm.pindirs, 0b01 << 14)
        self.assertEqual((sm.isr, sm.isr_count), (0x15, 5))
        self.assertEqual(sm.x, 1, "OUT PC jumped to 7")

    def test_pin_groups_stop_at_pin_15_single_pins_wrap(self):
        sm = sm_of("set pins, 7\nwait 1 in_base 15\nset x, 1" + HALT, set_base=14, set_count=3, in_base=1)
        run(sm, 2)
        self.assertEqual((sm.gpio_out, sm.x), (0b11 << 14, 0), "SET stops at pin 15; in_base + 15 = pin 0")
        sm.gpio_in = 1
        run(sm, 2)
        self.assertEqual(sm.x, 1)

    def test_reserved_osr_destination_discards(self):
        sm = sm_of(".word 0x60A4" + HALT)                       # out osr, 4
        sm.osr, sm.osr_count = 0xABCD, 0
        run(sm, 1)
        self.assertEqual((sm.osr, sm.osr_count, sm.x), (0x0ABC, 4, 0))

    def test_autopull(self):
        sm = sm_of("loop:\nout x, 8 auto\njmp loop")        # pull threshold 16: two bytes a word
        run(sm, 3)
        self.assertEqual((sm.pc, sm.x), (0, 0), "OSR and TX FIFO empty: waits")
        sm.tx_fifo.extend([0x2211, 0x4433])
        run(sm, 1)
        self.assertEqual((sm.pc, sm.osr_count), (0, 0), "loaded, issues again next strobe")
        xs = []
        for _ in range(4):
            run(sm, 2)
            xs.append(sm.x)
        self.assertEqual(xs, [0x11, 0x22, 0x33, 0x44], "reloaded in the strobe it empties: no gap")


class Alu(unittest.TestCase):

    def op(self, mnem, d, s):
        sm = run(sm_of(f"{mnem} x, y" + HALT) if True else None, 0)
        sm.x, sm.y = d, s
        return run(sm, 1).x

    def test_operations(self):
        d, s = 0xF0F0, 0x1234
        expect = {"mov": s, "rev": 0x2C48, "and": d & s, "or": d | s, "xor": d ^ s,
                  "xnor": ~(d ^ s) & 0xFFFF, "add": (d + s) & 0xFFFF, "sub": (d - s) & 0xFFFF}
        for mnem, value in expect.items():
            self.assertEqual(self.op(mnem, d, s), value, mnem)
        self.assertEqual(self.op("sub", 1, 2), 0xFFFF, "no borrow")

    def test_isr_osr_counts(self):
        """Both counts 0: the ISR empty, the OSR full."""
        sm = sm_of("in null, 5\nmov isr, x\nmov osr, x\npush iffull noblock\npush noblock" + HALT)
        sm.x = 7
        run(sm, 3)
        self.assertEqual((sm.isr, sm.isr_count, sm.osr, sm.osr_count), (7, 0, 7, 0))
        run(sm, 2)
        self.assertEqual(list(sm.rx_fifo), [7], "IfCond: not full, not pushed; a plain PUSH sends it")

    def test_pins_pindirs_cond_null(self):
        sm = sm_of("mov pins, x\nmov pindirs, x\nmov y, pins\nmov cond, osr\nmov null, x" + HALT)
        sm.x, sm.osr, sm.gpio_in = 0x81A5, 0x0242, 0x903C
        run(sm, 5)
        self.assertEqual((sm.gpio_out, sm.pindirs, sm.y), (0x81A5, 0x81A5, 0x903C), "all 16 pins")
        self.assertEqual(sm.cond[2], 0x42, "slot from bits [9:8]")

    def test_status_source(self):
        sm = sm_of("mov x, status" + HALT)
        expected = sm.status_word()
        self.assertEqual(run(sm, 1).x, expected)


class IrqFifo(unittest.TestCase):

    def test_irq_set_clear_wait(self):
        sm = run(sm_of("irq 3\nirq clear 3\nirq wait 5\nset x, 1" + HALT), 2)
        self.assertFalse(sm.irq_flags[3])
        run(sm, 3)
        self.assertTrue(sm.irq_flags[5] and sm.x == 0, "waits for the flag to be cleared")
        sm.irq_flags[5] = False
        run(sm, 2)
        self.assertEqual(sm.x, 1)

    def test_irq_peer(self):
        sm = run(sm_of("irq peer 2" + HALT), 1)
        self.assertEqual((sm.peer_irq_flags[2], sm.irq_flags[2]), (True, False))

    def test_push_pull_blocking(self):
        sm = sm_of("pull block\nset x, 1" + HALT)
        run(sm, 3)
        self.assertEqual(sm.x, 0)
        sm.tx_fifo.append(0xBEEF)
        run(sm, 2)
        self.assertEqual((sm.osr, sm.osr_count, sm.x), (0xBEEF, 0, 1))

    def test_nonblocking(self):
        sm = sm_of("pull noblock\npush noblock" + HALT)
        sm.x, sm.isr, sm.isr_count = 0x77, 0x55, 3
        sm.rx_fifo.extend([1, 2, 3, 4])
        run(sm, 2)
        self.assertEqual((sm.osr, sm.isr, sm.isr_count, list(sm.rx_fifo)), (0x77, 0, 0, [1, 2, 3, 4]),
                         "PULL copies X; PUSH drops but clears the ISR")

    def test_ifcond_thresholds(self):
        sm = sm_of(".push_thresh 8\npush iffull\nin null, 8\npush iffull" + HALT)
        run(sm, 1)
        self.assertEqual(len(sm.rx_fifo), 0)
        run(sm, 2)
        self.assertEqual(len(sm.rx_fifo), 1)


class CrcFeed(unittest.TestCase):

    def test_feeds_the_last_isr_bits_oldest_first(self):
        """A bit a strobe: crc_feed 8 takes 8 strobes."""
        for right, isr in ((True, 0xA500), (False, 0x00A5)):   # 0xA5 shifted in, both directions
            sm = sm_of(".crc width=8 poly=0x31\npau crc_feed, 8" + HALT, shift_in_right=right)
            sm.isr = isr
            run(sm, 7)
            self.assertEqual((sm.pc, sm.stalled), (0, True))
            run(sm, 1)
            self.assertEqual((sm.pc, sm.stalled), (1, False))
            ref = 0                                   # serial CRC-8, 0xA5 in arrival order
            bits = [0xA5 >> i & 1 for i in range(8)] if right else [0xA5 >> i & 1 for i in range(7, -1, -1)]
            for b in bits:
                fb = (ref >> 7 & 1) ^ b
                ref = (ref << 1) & 0xFF
                ref ^= 0x31 if fb else 0
            self.assertEqual(sm.crc >> 24, ref)                  # MSB-aligned


class CrcOfTheWire(unittest.TestCase):
    """The PAU CRC is fed with the data bits on the wire: a TX bit when it is sent (if it was
    queued with CRC on), an RX bit when it is decoded (CRC on, the PAU TX not driving)."""

    PAU = ".pau code=nrz baud=1\n.crc width=8 poly=0x07\n"     # a bit every 2 strobes

    @staticmethod
    def crc8(bits):
        ref = 0
        for b in bits:
            fb = (ref >> 7 & 1) ^ b
            ref = (ref << 1) & 0xFF ^ (0x07 if fb else 0)
        return ref

    def test_tx_bits_feed_when_sent(self):
        sm = sm_of(self.PAU + "set pau_ctl, tx|crc\nout pins, 8" + HALT)
        sm.osr = 0xA5
        run(sm, 2)
        self.assertEqual((sm.crc, len(sm.tx_queue)), (0, 8), "queued, not yet sent")
        run(sm, 1)                                       # bit 0 (a 1) starts
        self.assertEqual(sm.crc >> 24, self.crc8([1]))
        run(sm, 14)                                      # bit 7 starts on strobe 17
        self.assertEqual(sm.crc >> 24, self.crc8([0xA5 >> i & 1 for i in range(8)]))

    def test_append_after_the_data_bits(self):
        sm = sm_of(self.PAU + "set pau_ctl, tx|crc\nout pins, 8\npau crc_append" + HALT)
        sm.osr = 0xA5
        run(sm, 16)
        self.assertEqual((sm.pc, len(sm.tx_queue)), (2, 1), "waits while data bits are queued")
        run(sm, 1)
        ref = self.crc8([0xA5 >> i & 1 for i in range(8)])
        self.assertEqual((sm.pc, sm.crc >> 24), (3, ref))
        self.assertEqual(sm.tx_append, 8, "sent from the CRC register, not the queue")
        sent = []
        while sm.tx_append or sm.tx_active:
            left = sm.tx_append
            run(sm, 1)
            if sm.tx_append < left:
                sent.append(sm.tx_bit)
        self.assertEqual(sent, [ref >> i & 1 for i in range(7, -1, -1)], "MSB first")
        self.assertEqual(sm.crc, 0, "shifted out: fed its own top bit, the CRC only shifts")

    def test_the_flag_of_a_bit_is_taken_when_queued(self):
        sm = sm_of(self.PAU + "set pau_ctl, tx\nout pins, 8\nset pau_ctl, tx|crc" + HALT)
        sm.osr = 0xA5
        run(sm, 30)
        self.assertEqual((len(sm.tx_queue), sm.crc), (0, 0))

    def test_rx_does_not_feed_while_tx_drives(self):
        sm = sm_of(self.PAU + "set pau_ctl, tx\nout pins, 8\nset pau_ctl, rx|crc" + HALT)
        sm.osr = 0xA5
        run(sm, 2)
        while sm.tx_drives:                              # the TX pin looped back to RX
            self.assertEqual(sm.crc, 0)
            sm.gpio_in = sm.pins
            sm.step()
        self.assertGreater(len(sm.rx_queue), 4, "RX decoded the bits TX sent, without feeding them")


class Wait(unittest.TestCase):

    def test_level(self):
        sm = sm_of("wait 1 gpio 3\nset x, 1" + HALT)
        run(sm, 3)
        sm.gpio_in = 1 << 3
        run(sm, 2)
        self.assertEqual(sm.x, 1)

    def test_edge_needs_a_change_between_strobes(self):
        sm = sm_of("nop\nwait rise gpio 0\nset x, 1" + HALT)
        sm.gpio_in = 1
        run(sm, 4)                                       # high since before the WAIT
        self.assertEqual(sm.x, 0, "already high: no rising edge")
        sm.gpio_in = 0
        run(sm, 1)
        sm.gpio_in = 1
        run(sm, 2)
        self.assertEqual(sm.x, 1)

    def test_no_edge_on_the_first_strobe_after_reset(self):
        """The first strobe compares its values with themselves: a pin that rose since the
        RESET is no edge; one that rises after the first strobe is."""
        sm = sm_of("wait rise gpio 0\nset x, 1" + HALT)
        sm.gpio_in = 1                                   # low at RESET, high at the first strobe
        run(sm, 3)
        self.assertEqual((sm.pc, sm.x), (0, 0))
        sm.gpio_in = 0
        run(sm, 1)
        sm.gpio_in = 1
        run(sm, 2)
        self.assertEqual(sm.x, 1)

    def test_timeout(self):
        sm = sm_of("set timeout_val, 4\nwait 1 gpio 0 timeout\njmp timeout, 4\nnop\nset x, 1" + HALT)
        run(sm, 8)
        self.assertEqual((sm.timeout_flag, sm.x), (True, 1))

    def test_every_timed_wait_gets_the_whole_timeout(self):
        sm = sm_of("set timeout_val, 4\nwait 1 gpio 0 timeout\nset x, 1\nwait 1 gpio 0 timeout\nset y, 1"
                   + HALT)
        run(sm, 6)
        self.assertEqual((sm.timeout_flag, sm.pc), (True, 2), "issued at strobe 2, timed out at 6")
        run(sm, 6)
        self.assertEqual(sm.y, 0, "the second WAIT starts again from 4: TIMEOUT_VAL is kept")
        self.assertEqual(run(sm, 1).y, 1)

    def test_timeout_scaled_by_dlymul(self):
        sm = sm_of("set dlymul, 10\nset timeout_val, 3\nwait 1 gpio 0 timeout\nset x, 1" + HALT)
        run(sm, 32)
        self.assertEqual(sm.timeout_flag, False, "issued at strobe 3: 30 strobes")
        run(sm, 1)
        self.assertEqual((sm.timeout_flag, sm.pc), (True, 3))

    def test_a_pin_changed(self):
        """True in the strobe where `gpio_in` differs from the previous strobe's, then no more."""
        sm = sm_of("wait 1 status pins_changed\nset x, 1\njmp status pins_changed, 0\nset y, 1" + HALT)
        run(sm, 3)
        self.assertEqual(sm.x, 0, "nothing changes: waiting")
        sm.gpio_in = 1 << 12
        run(sm, 4)                                   # WAIT ends, set x, jmp not taken, set y
        self.assertEqual((sm.x, sm.y), (1, 1), "seen in one strobe, not in the next")

    def test_edge_between_the_values_two_strobes_see(self):
        """IRQ 0 is clear when `irq 0` runs and set when the WAIT runs: a rising edge."""
        sm = sm_of("irq 0\nwait rise irq 0\nset x, 1" + HALT)
        run(sm, 3)
        self.assertEqual(sm.x, 1)

    def test_polarity_on_an_inverted_condition(self):
        sm = sm_of(".cond1 !gpio 2\njmp cond1, 3\nwait 1 gpio 2\nset x, 1" + HALT)
        sm.gpio_in = 1 << 2
        run(sm, 3)
        self.assertEqual(sm.x, 1, "WAIT reuses the inverted slot with its own polarity")


class Time(unittest.TestCase):

    def test_in_time_counts_strobes(self):
        """The third strobe reads 2; with CLKDIV 3, still strobes, not clocks."""
        for clkdiv in (1, 3):
            sm = StateMachine(assemble("nop\nnop\nin time, 16\npush noblock" + HALT), clkdiv_int=clkdiv)
            for _ in range(4 * clkdiv):
                sm.step()
            self.assertEqual(list(sm.rx_fifo), [2])
        sm.reset()
        self.assertEqual(sm.time, 0, "RESET clears it")


class Set(unittest.TestCase):

    def test_pins_and_pindirs_window(self):
        sm = run(sm_of("set pins, 31\nset pindirs, 5" + HALT, set_base=4, set_count=3), 2)
        self.assertEqual((sm.gpio_out, sm.pindirs), (0x70, 0x50))

    def test_dlymul_scales_delays(self):
        sm = sm_of("set dlymul, 3\nset x, 1 [2]\nset x, 2" + HALT)
        run(sm, 2)
        self.assertEqual(sm.x, 1)
        run(sm, 6)
        self.assertEqual(sm.x, 1, "2 x 3 delay strobes")
        self.assertEqual(run(sm, 1).x, 2)

    def test_clkdiv_does_not_scale_delays(self):
        sm = StateMachine(assemble("set x, 1 [2]\nset x, 2" + HALT), clkdiv_int=3)
        clocks = []
        for c in range(1, 20):
            sm.step()
            if sm.x and len(clocks) < sm.x:
                clocks.append(c)
        self.assertEqual(clocks, [3, 12], "a strobe every 3 clocks; [2] = 2 strobes")

    def test_pau_baud_zero_is_32(self):
        sm = run(sm_of("set pau_baud, 0" + HALT), 1)
        self.assertEqual(sm._half, 32)


class SideSetDelay(unittest.TestCase):

    def test_sideset_with_delay_bits(self):
        sm = sm_of(".side_set 2\nnop side 3 [7]\nset x, 1" + HALT, sideset_base=3)
        run(sm, 1)
        self.assertEqual(sm.gpio_out, 0b11000)
        run(sm, 7)
        self.assertEqual(sm.x, 0)
        self.assertEqual(run(sm, 1).x, 1)

    def test_optional_sideset(self):
        sm = run(sm_of(".side_set 1 opt\nnop side 1\nnop\nnop side 0" + HALT), 2)
        self.assertEqual(sm.gpio_out, 1, "no side-set on the second nop")
        self.assertEqual(run(sm, 1).gpio_out, 0)

    def test_sideset_pindirs(self):
        sm = run(sm_of(".side_set 1 pindirs\nnop side 1" + HALT, sideset_base=6), 1)
        self.assertEqual((sm.pindirs, sm.gpio_out), (0x40, 0))


class Prescaler(unittest.TestCase):

    def strobes(self, int_, frac, clocks):
        sm = StateMachine(assemble("loop:\nin null, 1\njmp loop"), clkdiv_int=int_, clkdiv_frac=frac)
        out = []
        for _ in range(clocks):
            sm.step()
            out.append(int(sm.strobed))
        return out

    def test_integer(self):
        self.assertEqual(self.strobes(3, 0, 9), [0, 0, 1] * 3)

    def test_fraction(self):
        # 1 + 128/256 = 1.5 clocks per strobe on average: periods alternate 1, 2
        self.assertEqual(sum(self.strobes(1, 128, 30)), 20)


class Reserved(unittest.TestCase):

    def test_reserved_encodings_are_nops(self):
        for word in (0xC0C1, 0xC041, 0xC0A4):            # CRC reset with a count, FIFO bits [1:0], WAIT bit 2
            with self.subTest(word=hex(word)):
                sm = sm_of(f".word {word:#x}" + HALT)
                sm.x, sm.isr, sm.osr = 1, 2, 3
                run(sm, 1)
                self.assertEqual((sm.pc, sm.x, sm.isr, sm.osr, sm.stalled), (1, 1, 2, 3, False))
                self.assertEqual((list(sm.rx_fifo), any(sm.irq_flags)), ([], False))


class Reset(unittest.TestCase):

    def test_reset_state(self):
        sm = StateMachine(assemble("nop"))
        self.assertEqual((sm.pc, sm.isr_count, sm.osr_count, sm.cond, sm.pau_ctl), (0, 0, 16, [0x60, 0x61, 0x00, 0xE4], 0))


if __name__ == "__main__":
    unittest.main()
