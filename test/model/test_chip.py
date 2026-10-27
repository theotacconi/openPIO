#!/usr/bin/env python3
"""Chip model (sim/model/chip.py): host interface of HOST.md and the multi-SM rules of ISA.md.

    python3 test/model/test_chip.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sim" / "model"))
from asm import assemble                                             # noqa: E402
from chip import (Chip, header, CTRL, GPIO, ID, IRQ, PC, PINS, STATUS, WRAP, X,       # noqa: E402
                  COND_CFG01, COND_CFG23, PINBASE, PIN_MASK, MISC, CLKDIV, SM_STATUS,
                  OP_IMEM_RD, OP_IMEM_WR, OP_REG_RD, OP_REG_WR, OP_RX_POP, OP_TX_PUSH)


class Host(unittest.TestCase):

    def test_reset_values_and_id(self):
        chip = Chip(2)
        regs = chip.spi(header(OP_REG_RD, 1, WRAP), [0] * 6)[1:]
        self.assertEqual(regs, [0x3F00, 0, 0x5000, 0xFF, 0x0003, 0x0001])   # WRAP .. CLKDIV
        self.assertEqual(chip.reg_read(0, COND_CFG01), 0x6160)
        self.assertEqual(chip.reg_read(0, COND_CFG23), 0xE400)
        self.assertEqual(chip.reg_read(0, ID), 0x2250)

    def test_burst_imem_and_fifos(self):
        chip = Chip(2)
        prog = assemble("pull block\nmov isr, osr\npush block")
        chip.spi(header(OP_IMEM_WR, 1, 0), list(prog))
        self.assertEqual(chip.spi(header(OP_IMEM_RD, 1, 0), [0] * 3)[1:], list(prog))
        chip.reg_write(1, WRAP, 2 << 8)
        chip.spi(header(OP_TX_PUSH, 1), [0x1111, 0x2222, 0x3333, 0x4444, 0x5555])   # 5th dropped
        chip.reg_write(1, CTRL, 0b10)
        chip.step(40)
        self.assertEqual(chip.spi(header(OP_RX_POP, 1), [0] * 5)[1:], [0x1111, 0x2222, 0x3333, 0x4444, 0])

    def test_registers_written_only_while_stopped(self):
        chip = Chip(2)
        chip.reg_write(0, X, 0x1234)
        chip.reg_write(0, CTRL, 1)
        chip.reg_write(0, X, 0x5678)              # running: ignored
        self.assertEqual(chip.reg_read(0, X), 0x1234)


class MultiSm(unittest.TestCase):

    def test_synchronized_start(self):
        chip = Chip(2)
        chip.load(0, assemble("set pins, 1\nend: jmp end"), set_base=0, set_count=1)
        chip.load(1, assemble("set pins, 1\nend: jmp end"), set_base=1, set_count=1)
        chip.reg_write(0, CTRL, 0b11)             # both on the same clock
        chip.step()
        self.assertEqual(chip.bus.out & 3, 3)

    def test_highest_sm_wins_and_pin_mask(self):
        chip = Chip(2)
        chip.load(0, assemble("set pins, 1\nend: jmp end"), set_count=1)
        chip.load(1, assemble("set pins, 0\nend: jmp end"), set_count=1)
        chip.reg_write(0, CTRL, 0b11)
        chip.step()
        self.assertEqual(chip.bus.out & 1, 0, "SM 1 wins pin 0")
        chip.reg_write(0, CTRL, 0b110000)          # reset both (stopped)
        chip.reg_write(1, PIN_MASK, 0xFE)          # SM 1 may not touch pin 0
        chip.reg_write(0, CTRL, 0b11)
        chip.step()
        self.assertEqual(chip.bus.out & 1, 1, "PIN_MASK keeps SM 1 off pin 0")

    def test_push_peer_bridge(self):
        """SM 0 doubles every word and pushes it to SM 1, which adds 1: no host in between."""
        chip = Chip(2)
        chip.load(0, assemble("""
            .wrap_target
                pull block
                mov x, osr
                add x, x
                mov isr, x
                push peer
            .wrap
        """))
        chip.load(1, assemble("""
            .wrap_target
                pull block
                set y, 1
                mov x, osr
                add x, y
                mov isr, x
                push
            .wrap
        """))
        chip.reg_write(0, CTRL, 0b11)
        for w in (1, 2, 3):
            chip.tx_push(0, w)
        chip.step(60)
        self.assertEqual([chip.rx_pop(1) for _ in range(3)], [3, 5, 7])

    def test_pull_peer_and_peer_irq(self):
        chip = Chip(2)
        chip.load(0, assemble("""
                pull peer          ; from SM 1's RX FIFO
                mov x, osr
                irq peer 2         ; flag 2 of SM 1
            end:
                jmp end
        """))
        chip.load(1, assemble("""
                set x, 9
                mov isr, x
                push
                wait 1 irq 2       ; its own flag, set by SM 0
                irq 0
            end:
                jmp end
        """))
        chip.reg_write(0, CTRL, 0b11)
        chip.step(20)
        self.assertEqual(chip.sms[0].x, 9)
        self.assertTrue(chip.sms[1].irq_flags[0])
        self.assertEqual(chip.reg_read(0, 0x24), 0b10)          # IRQ_SUM: only SM 1

    def test_debug_write_and_step(self):
        chip = Chip(2)
        chip.load(0, assemble("""
            start:
                jmp x--, start
                irq 0
            end:
                jmp end
        """))
        chip.reg_write(0, X, 2)
        chip.reg_write(0, PC, 0)
        for _ in range(3):                         # x = 2, 1, 0: three passes of jmp x--
            chip.reg_write(0, CTRL, 1 << 8)
        self.assertEqual(chip.reg_read(0, PC), 1)
        chip.reg_write(0, CTRL, 1 << 8)
        self.assertTrue(chip.sms[0].irq_flags[0])

    def test_open_drain_is_per_pin(self):
        chip = Chip(2)
        chip.load(0, assemble("set pins, 2\nset pindirs, 3\nend: jmp end"), set_count=2)
        chip.reg_write(0, GPIO, 0x07)              # pins 0, 1, 2 open drain
        chip.reg_write(0, CTRL, 1)
        chip.step()
        self.assertEqual(chip.pins[1] & 7, 0, "latch 0 but not enabled: nothing driven")
        chip.step()
        out, oe = chip.pins
        self.assertEqual((out & 7, oe & 7), (0, 0b001), "pin 0 pulled low, pin 1 floats, pin 2 off")

    def test_status_word(self):
        chip = Chip(2)
        chip.load(0, assemble("mov x, status\nend: jmp end"))
        chip.reg_write(0, CTRL, 1)
        chip.step()
        # SM: X == 0, Y == 0, X == Y, OSR empty, TX FIFO empty
        # PAU: TX idle, RX idle, TX room, CRC == CRC_RES and CRC == 0 (both 0)
        self.assertEqual(chip.sms[0].x, 0x3757)
        self.assertEqual(chip.reg_read(0, SM_STATUS), 0x3752)   # X != 0 now: bits 0, 2 drop


if __name__ == "__main__":
    unittest.main()
