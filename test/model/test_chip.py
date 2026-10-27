#!/usr/bin/env python3
"""Chip model (sim/model/chip.py): host interface of HOST.md and the multi-SM rules of ISA.md.

    python3 test/model/test_chip.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sim" / "model"))
from asm import assemble                                             # noqa: E402
from chip import (Chip, header, CTRL, GPIO, ID, IRQ, IRQ_MASK, PC, PINS, STATUS, WRAP, X, FIFO_LEVEL,  # noqa: E402
                  COND_CFG01, COND_CFG23, CRC_LO, IRQ_SUM, PINBASE, PIN_MASK, MISC, CLKDIV, SM_STATUS,
                  OP_IMEM_RD, OP_IMEM_WR, OP_REG_RD, OP_REG_WR, OP_RX_POP, OP_TX_PUSH)
from host import Host                                                # noqa: E402


def load(chip, n, program, **cfg):
    Host(chip).load(n, program, **cfg)


class HostInterface(unittest.TestCase):

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
        chip.spi(header(OP_TX_PUSH, 1), [0x1111, 0x2222, 0x3333, 0x4444, 0x5555])
        chip.reg_write(1, CTRL, 0b10)
        chip.step(40)
        self.assertEqual(chip.spi(header(OP_RX_POP, 1), [0] * 6)[1:], [0x1111, 0x2222, 0x3333, 0x4444, 0x5555, 0])

    def test_fifo_depth_through_the_sram(self):
        chip = Chip(2)
        load(chip, 0, assemble("loop:\npull block\nmov isr, osr\npush block\njmp loop"))
        accepted = [chip.tx_push(0, w) for w in range(134)]
        self.assertEqual(accepted, [True] * 132 + [False] * 2, "4 front + 128 SRAM words")
        self.assertEqual(chip.reg_read(0, FIFO_LEVEL), 132)
        chip.reg_write(0, CTRL, 1)
        out = []
        for _ in range(2000):
            chip.step()
            if chip.sms[0].rx_level:
                out.append(chip.rx_pop(0))
        self.assertEqual(out, list(range(132)), "order kept across front and SRAM")

    def test_host_sets_a_flag_and_masks_the_irq_pin(self):
        chip = Chip(2)
        load(chip, 0, assemble("wait 1 irq 0\nirq clear 0\nirq 3\nend: jmp end"))
        chip.reg_write(0, IRQ_MASK, 1 << 3)               # only flag 3 reaches HOST_IRQ
        chip.reg_write(0, CTRL, 1)
        chip.step(5)
        self.assertEqual(chip.reg_read(0, PC), 0, "waiting for the host")
        chip.reg_write(0, IRQ, 1 << 8)                    # host sets flag 0
        self.assertEqual(chip.host_irq, 0, "flag 0 is masked off the pin")
        chip.step(5)
        self.assertEqual((chip.reg_read(0, IRQ), chip.host_irq), (1 << 3, 1))

    def test_host_load_configures_through_registers(self):
        chip = Chip(2)
        chip.reg_write(0, CTRL, 0b11)             # both running: load stops SM 1 only
        load(chip, 1, assemble("""
            .in_pins 6
            .out_pins 2
            .set_pins 3 2
            .side_set 1 opt
            .sideset_pins 5
            .pin_mask 2, 3, 4, 5
            .open_drain 3
            .shift_in left
            .push_thresh 8
            .clkdiv 3 128
            .x 0x1234
            .y 7
            .start go
                nop
            go:
            .wrap_target
                jmp go
            .wrap
        """), out_base=1)                         # an override
        self.assertEqual(chip.reg_read(0, CTRL), 0b01)
        regs = chip.spi(header(OP_REG_RD, 1, WRAP), [0] * 6)[1:]
        self.assertEqual(regs, [0x0101, 0x0509, 0x2316, 0x3C, 0x0802, 0x8003])   # WRAP .. CLKDIV
        self.assertEqual(chip.spi(header(OP_REG_RD, 1, PC), [0] * 3)[1:], [1, 0x1234, 7])
        self.assertEqual(chip.reg_read(0, GPIO), 1 << 3)
        with self.assertRaises(TypeError):
            load(chip, 1, assemble("nop"), in_bsae=1)

    def test_jmp_pin_moved_by_the_host(self):
        """`jmp pin` gets a GPIO condition slot; the host's jmp_pin picks the pin."""
        chip = Chip(2)
        prog = assemble("wait 1 pin\njmp pin, high\nirq 0\nend: jmp end\nhigh: irq 1\njmp end")
        load(chip, 0, prog, jmp_pin=6)
        slot = prog.pin_slot
        self.assertEqual(chip.reg_read(0, COND_CFG01 + slot // 2) >> 8 * (slot % 2) & 0xFF, 0x06)
        chip.uio_in = 1 << 0                        # the default pin: ignored
        chip.reg_write(0, CTRL, 1)
        chip.step(8)
        self.assertEqual(chip.reg_read(0, PC), 0, "waiting for pin 6")
        chip.uio_in = 1 << 6
        chip.step(8)
        self.assertEqual(chip.sms[0].irq_flags[:2], [False, True], "pin 6 high: jumped")

    def test_fifo_requests_on_the_irq_pin(self):
        chip = Chip(2)
        load(chip, 0, assemble("end: jmp end"))
        chip.reg_write(0, IRQ_MASK, 1 << 9)             # TX room only
        self.assertEqual(chip.host_irq, 1, "TX FIFO empty: room")
        chip.spi(header(OP_TX_PUSH, 0), list(range(65)))
        self.assertEqual(chip.host_irq, 0, "65 words: full enough")
        chip.reg_write(0, IRQ_MASK, 1 << 8)             # RX data only
        self.assertEqual(chip.host_irq, 0)
        chip.sms[0].rx_fifo.append(7)
        self.assertEqual((chip.host_irq, chip.reg_read(0, IRQ_SUM)), (1, 1))
        chip.rx_pop(0)
        self.assertEqual(chip.host_irq, 0, "drained: ends by itself")

    def test_reset_clears_flags_and_loads_the_crc_init(self):
        chip = Chip(2)
        load(chip, 0, assemble(".crc width=16 poly=0x8005 init=ones\nend: jmp end"))
        self.assertEqual(chip.reg_read(0, CRC_LO), 0xFFFF, "CRC initial value after RESET")
        chip.reg_write(0, IRQ, 0x81 << 8)
        self.assertEqual(chip.sms[1].peer_irq_flags[7], True, "the peer sees the flag")
        chip.reg_write(0, CTRL, 1 << 4)                          # RESET SM 0
        self.assertEqual((chip.reg_read(0, IRQ), chip.sms[1].peer_irq_flags[7]), (0, False))

    def test_clkdiv_up_to_255(self):
        chip = Chip(2)
        load(chip, 0, assemble("loop:\nin null, 1\njmp loop"), clkdiv_int=200, clkdiv_frac=128)
        self.assertEqual(chip.reg_read(0, CLKDIV), 0x80C8)
        chip.reg_write(0, CTRL, 1)
        strobes = 0
        for _ in range(2010):
            chip.step()
            strobes += chip.sms[0].strobed
        self.assertEqual(strobes, 10, "one strobe every 200.5 clocks")

    def test_registers_written_only_while_stopped(self):
        chip = Chip(2)
        chip.reg_write(0, X, 0x1234)
        chip.reg_write(0, CTRL, 1)
        chip.reg_write(0, X, 0x5678)              # running: ignored
        self.assertEqual(chip.reg_read(0, X), 0x1234)


class MultiSm(unittest.TestCase):

    def test_synchronized_start(self):
        chip = Chip(2)
        load(chip, 0, assemble("set pins, 1\nend: jmp end"), set_base=0, set_count=1)
        load(chip, 1, assemble("set pins, 1\nend: jmp end"), set_base=1, set_count=1)
        chip.reg_write(0, CTRL, 0b11)             # both on the same clock
        chip.step()
        self.assertEqual(chip.bus.out & 3, 3)

    def test_highest_sm_wins_and_pin_mask(self):
        chip = Chip(2)
        load(chip, 0, assemble("set pins, 1\nend: jmp end"), set_count=1)
        load(chip, 1, assemble("set pins, 0\nend: jmp end"), set_count=1)
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
        load(chip, 0, assemble("""
            .wrap_target
                pull block
                mov x, osr
                add x, x
                mov isr, x
                push peer
            .wrap
        """))
        load(chip, 1, assemble("""
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
        load(chip, 0, assemble("""
                pull peer          ; from SM 1's RX FIFO
                mov x, osr
                irq peer 2         ; flag 2 of SM 1
            end:
                jmp end
        """))
        load(chip, 1, assemble("""
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

    def test_wait_on_a_peer_flag(self):
        chip = Chip(2)
        load(chip, 0, assemble("wait 1 peer_irq 5\nirq 0\nend: jmp end"))
        chip.reg_write(0, CTRL, 0b01)
        chip.step(5)
        self.assertEqual(chip.reg_read(0, PC), 0, "waiting")
        chip.reg_write(1, IRQ, 1 << 13)            # host sets flag 5 of SM 1
        chip.step(3)
        self.assertTrue(chip.sms[0].irq_flags[0])

    def test_push_peer_to_a_full_fifo(self):
        """The peer's TX FIFO (132 words) full: PUSH PEER stalls; NOBLOCK drops the word."""
        chip = Chip(2)
        for n, push in ((0, "push peer"), (1, "push peer noblock")):
            load(chip, n, assemble(f"set x, {n + 1}\nmov isr, x\n{push}\nirq 0\nend: jmp end"))
        for n in (0, 1):
            for w in range(132):
                chip.tx_push(n, w)
        chip.reg_write(0, CTRL, 0b11)
        chip.step(10)
        self.assertEqual((chip.reg_read(0, PC), chip.sms[0].stalled), (2, True), "SM 0 stalls")
        self.assertEqual((chip.sms[1].irq_flags[0], chip.sms[1].isr), (True, 0), "SM 1 drops it")
        chip.sms[1].tx_fifo.popleft()               # SM 1 takes a word: room for SM 0's
        chip.step(10)
        self.assertTrue(chip.sms[0].irq_flags[0])
        self.assertEqual(chip.reg_read(1, FIFO_LEVEL) & 0xFF, 132)

    def test_pull_ifempty_waits_for_the_threshold(self):
        chip = Chip(2)
        load(chip, 0, assemble("""
            .pull_thresh 8
                pull block
                out x, 4
                pull ifempty      ; 4 bits used of 8: keeps the OSR
                out y, 4
                pull ifempty      ; 8 used: loads the next word
                out isr, 16
            end:
                jmp end
        """))
        chip.spi(header(OP_TX_PUSH, 0), [0x0021, 0xBEEF])
        chip.reg_write(0, CTRL, 1)
        chip.step(10)
        self.assertEqual((chip.sms[0].x, chip.sms[0].y, chip.sms[0].isr), (1, 2, 0xBEEF))

    def test_debug_write_and_step(self):
        chip = Chip(2)
        load(chip, 0, assemble("""
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
        load(chip, 0, assemble("set pins, 2\nset pindirs, 3\nend: jmp end"), set_count=2)
        chip.reg_write(0, GPIO, 0x07)              # pins 0, 1, 2 open drain
        chip.reg_write(0, CTRL, 1)
        chip.step()
        self.assertEqual(chip.pins[1] & 7, 0, "latch 0 but not enabled: nothing driven")
        chip.step()
        out, oe = chip.pins
        self.assertEqual((out & 7, oe & 7), (0, 0b001), "pin 0 pulled low, pin 1 floats, pin 2 off")

    def test_status_word(self):
        chip = Chip(2)
        load(chip, 0, assemble("mov x, status\nend: jmp end"))
        chip.reg_write(0, CTRL, 1)
        chip.step()
        # SM: X == 0, Y == 0, X == Y, OSR empty, TX FIFO empty
        # PAU: TX idle, RX idle, TX room, CRC == CRC_RES and CRC == 0 (both 0)
        self.assertEqual(chip.sms[0].x, 0x3757)
        self.assertEqual(chip.reg_read(0, SM_STATUS), 0x3752)   # X != 0 now: bits 0, 2 drop


if __name__ == "__main__":
    unittest.main()
