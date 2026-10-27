#!/usr/bin/env python3
"""Assembler (sim/model/asm.py): every instruction word round-trips through the disassembler,
bad sources are rejected, directives fill the configuration.

    python3 test/model/test_asm.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sim" / "model"))
from asm import CONFIG_RESET, AssemblerError, assemble, disassemble    # noqa: E402
from emu import StateMachine                                            # noqa: E402


class RoundTrip(unittest.TestCase):

    def test_every_word(self):
        """disassemble -> assemble -> disassemble gives the same text for all 65536 words (bits
        the text does not show, as a NOP's target, assemble to 0). Reserved words, NOPs when
        executed (ISA.md), disassemble to .word."""
        for count, opt in ((0, False), (1, False), (2, True)):
            header = f".side_set {count}{' opt' if opt else ''}\n" if count else ""
            valid = 0
            for word in range(1 << 16):
                text = disassemble(word, count, opt)
                if "reserved" in text or text.startswith(".word"):
                    continue
                valid += 1
                again = disassemble(assemble(header + text)[0], count, opt)
                if again != text:
                    self.fail(f"0x{word:04x}: {text!r} -> {again!r} (side_set {count})")
            self.assertEqual(valid, 59488, "90.8 % of the words are instructions, the rest reserved")

    def test_reserved_words_are_nops(self):
        """What the disassembler calls reserved, the emulator executes as a NOP."""
        def state(sm):
            return (sm.x, sm.y, sm.isr, sm.osr, sm.isr_count, sm.osr_count, list(sm.irq_flags),
                    sm.gpio_out, sm.pindirs, sm.crc, list(sm.tx_queue), sm.pau_ctl, sm.stalled,
                    list(sm.tx_fifo), list(sm.rx_fifo))
        for word in range(1 << 16):
            if not word & 0x1F00 and disassemble(word).startswith(".word"):    # no delay
                sm = StateMachine([word])
                sm.x, sm.y, sm.isr, sm.osr, sm.isr_count, sm.osr_count = 1, 2, 3, 4, 5, 6
                sm.tx_fifo.append(7)
                before = state(sm)
                sm.step()
                self.assertEqual((state(sm), sm.pc), (before, 1), f".word 0x{word:04x}")


class Errors(unittest.TestCase):

    BAD = {
        "unknown mnemonic": "foo x",
        "unknown directive": ".foo 1",
        "delay too long": "nop [32]",
        "delay and side-set too long": ".side_set 2\nnop side 1 [8]",
        "side without .side_set": "nop side 1",
        "jmp target": "jmp 64",
        "unknown label": "jmp nowhere",
        "duplicate label": "a:\nnop\na:\nnop",
        "set value": "set x, 32",
        "five conditions": "jmp !x 1, 0\njmp !x 2, 0\njmp !x 3, 0\njmp !x 4, 0\njmp !x 5, 0",
        "too long": "nop\n" * 65,
        "pin out of range": ".pin_mask 1, 8",
        "set count": ".set_pins 0 6",
        "shift direction": ".shift_in up",
        "threshold": ".push_thresh 17",
        "clkdiv": ".clkdiv 256",
        "start label": ".start nowhere\nnop",
        "pau encoding": ".pau enc=fm",
        "crc width": ".crc width=33 poly=1",
    }

    def test_rejected(self):
        for what, src in self.BAD.items():
            with self.subTest(what), self.assertRaises(AssemblerError):
                assemble(src)


class Directives(unittest.TestCase):

    def test_config(self):
        prog = assemble("""
            .set_pins 2 3
            .pin_mask 2, 3, 4
            .open_drain 3
            .shift_out left
            .x 0xBEEF
            .start loop
                nop
            loop:
                jmp loop
        """)
        cfg = prog.config()
        self.assertEqual((cfg["set_base"], cfg["set_count"], cfg["pin_mask"], cfg["open_drain_mask"]),
                         (2, 3, 0b11100, 0b1000))
        self.assertEqual((cfg["shift_out_right"], cfg["shift_in_right"]), (False, True))
        self.assertEqual((cfg["x"], cfg["y"], cfg["start"]), (0xBEEF, 0, 1))
        self.assertEqual((cfg["wrap_bottom"], cfg["wrap_top"]), (0, 1))
        unset = set(CONFIG_RESET) - set(prog.cfg)
        self.assertEqual({k: cfg[k] for k in unset}, {k: CONFIG_RESET[k] for k in unset})

    def test_every_program_assembles(self):
        for path in sorted(Path(__file__).resolve().parents[2].glob("programs/*/*.asm")):
            with self.subTest(path.name):
                self.assertLessEqual(len(assemble(path.read_text())), 64)


if __name__ == "__main__":
    unittest.main()
