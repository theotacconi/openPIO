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
                  COND_CFG01, COND_CFG23, CRC_LO, IRQ_SUM, PINBASE, PIN_MASK, MISC, CLKDIV, CLKDIV_FRAC,
                  SM_STATUS, PINS_IN, PINS_OE, PADS_BIDIR, PADS_IN, PADS_OUT,
                  OP_IMEM_RD, OP_IMEM_WR, OP_REG_RD, OP_REG_WR, OP_RX_POP, OP_TX_PUSH)
from host import Host                                                # noqa: E402


def load(chip, n, program, **cfg):
    Host(chip).load(n, program, **cfg)


class HostInterface(unittest.TestCase):

    def test_reset_values_and_id(self):
        chip = Chip(2)
        regs = chip.spi(header(OP_REG_RD, 1, WRAP), [0] * 6)[1:]
        self.assertEqual(regs, [0x3F00, 0, 0x5000, 0xFFFF, 0x0003, 0x0001])   # WRAP .. CLKDIV
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
        accepted = []
        for w in range(135):                       # within 4 clocks the IN register empties
            accepted.append(chip.tx_push(0, w))
            chip.step(4)
        self.assertEqual(accepted, [True] * 133 + [False] * 2, "4 front + 128 SRAM + IN")
        self.assertEqual(chip.reg_read(0, FIFO_LEVEL), 133)
        self.assertEqual(chip.tx_push(0, 0), False, "full")
        chip.reg_write(0, CTRL, 1)
        out = []
        for _ in range(3000):
            chip.step()
            if (word := chip.sms[0].rx_read()) is not None:
                out.append(word)
        self.assertEqual(out, list(range(133)), "order kept across front, SRAM and registers")

    def test_random_traffic_keeps_order(self):
        """The host writes and reads SM 0 at random clocks, SM 0 adds 1 to every word: each
        arrives once, in order, through front, SRAM, IN and OUT registers of both FIFOs."""
        import random
        for seed, clkdiv in ((1, 1), (2, 1), (3, 2)):
            rng = random.Random(seed)
            chip = Chip(2)
            load(chip, 0, assemble(".y 1\n.wrap_target\npull block\nmov x, osr\nadd x, y\n"
                                   "mov isr, x\npush block\n.wrap"), clkdiv_int=clkdiv)
            chip.reg_write(0, CTRL, 0b01)
            sent, got, levels = 0, [], set()
            for _ in range(6000):
                if rng.random() < 0.5 and sent < 3000 and chip.tx_push(0, sent):
                    sent += 1
                if rng.random() < 0.05 and (w := chip.sms[0].rx_read()) is not None:
                    got.append(w)
                chip.step()
                levels.add((chip.sms[0].tx_level, chip.sms[0].rx_level))
            self.assertEqual(got, list(range(1, len(got) + 1)), f"seed {seed}")
            self.assertGreater(len(got), 250)
            self.assertEqual([max(x) for x in zip(*levels)], [133] * 2, "both FIFOs filled up")

    def test_sram_port_serves_every_request_within_4_clocks(self):
        sm = Chip(2).sms[0]
        sm.tx_back.extend(range(10, 20))           # TX refill pending (front empty)
        sm.tx_in = 99                              # TX store pending
        sm.rx_fifo.extend([1, 2])                  # RX store pending
        sm.rx_back.extend([5, 6])                  # RX fetch pending (OUT empty)
        for _ in range(4):
            sm.buffer_clock()
        self.assertEqual((sm.tx_in, sm.tx_back[-1], len(sm.rx_fifo), sm.rx_out is not None
                          or sm.rx_flight is not None, len(sm.tx_fifo) + (sm.tx_flight is not None)),
                         (None, 99, 1, True, 1), "one of each, in turn")

    def test_imem_read_only_while_stopped(self):
        chip = Chip(2)
        load(chip, 0, assemble("nop\nend: jmp end"))
        self.assertEqual(chip.spi(header(OP_IMEM_RD, 0, 1), [0])[1:], [0x0001])
        chip.reg_write(0, CTRL, 1)
        self.assertEqual(chip.spi(header(OP_IMEM_RD, 0, 1), [0])[1:], [0], "the port fetches")

    def test_writes_faster_than_the_sram_wait(self):
        """Two writes in one clock: the second finds the IN register busy."""
        chip = Chip(2)
        self.assertTrue(chip.tx_push(0, 0))
        self.assertFalse(chip.tx_push(0, 1), "IN busy this clock")
        chip.step()
        self.assertTrue(chip.tx_push(0, 1), "IN stored into the SRAM at the clock's end")

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
        self.assertEqual(regs, [0x0101, 0x0509, 0x2316, 0x3C, 0x0802, 3])   # WRAP .. CLKDIV
        self.assertEqual(chip.reg_read(1, CLKDIV_FRAC), 128)
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
        chip.pin_in = 1 << 0                        # the default pin: ignored
        chip.reg_write(0, CTRL, 1)
        chip.step(8)
        self.assertEqual(chip.reg_read(0, PC), 0, "waiting for pin 6")
        chip.pin_in = 1 << 6
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
        chip.step(4)                                    # front, SRAM, OUT register
        self.assertEqual(chip.rx_pop(0), 7)
        self.assertEqual(chip.host_irq, 0, "drained: ends by itself")

    def test_reset_clears_flags_and_loads_the_crc_init(self):
        chip = Chip(2)
        load(chip, 0, assemble(".crc width=16 poly=0x8005 init=ones\nend: jmp end"))
        self.assertEqual(chip.reg_read(0, CRC_LO), 0xFFFF, "CRC initial value after RESET")
        chip.reg_write(0, IRQ, 0x81 << 8)
        chip.step()
        self.assertEqual(chip.sms[1].peer_irq_flags[7], True, "the peer sees the flag")
        chip.reg_write(0, CTRL, 1 << 4)                          # RESET SM 0
        chip.step()
        self.assertEqual((chip.reg_read(0, IRQ), chip.sms[1].peer_irq_flags[7]), (0, False))

    def test_peers_act_in_parallel(self):
        """An SM sets its peer's flag 0 in clock 1 while the peer tests it: seen in clock 2, not
        1, whichever SM writes."""
        setter = "irq peer 0\nh: jmp h"
        tester = "jmp irq 0, 3\njmp irq 0, 5\nh: jmp h\nset x, 1\nh1: jmp h1\nset x, 2\nh2: jmp h2"
        for n in (0, 1):
            chip = Chip(2)
            load(chip, n, assemble(setter))
            load(chip, 1 - n, assemble(tester))
            chip.reg_write(0, CTRL, 0b11)
            chip.step(4)
            self.assertEqual(chip.sms[1 - n].x, 2, f"SM {n} -> SM {1 - n}: seen from the next clock")

    def test_host_access_in_one_clock(self):
        """A host word lands in the IN register at the end of its clock, then passes the SRAM:
        a PULL in that clock finds the FIFO empty, the word is in the front 3 clocks later
        (store, read, landing). A pushed word: stored and read in the clocks after its own."""
        chip = Chip(2)
        load(chip, 0, assemble("set x, 5\npull noblock\nmov y, osr\npull block\nh: jmp h"))
        chip.reg_write(0, CTRL, 1)
        chip.step()
        chip.host(OP_TX_PUSH, 0, word=0xABCD)                 # clock 2, the first PULL
        chip.step(3)
        self.assertEqual((chip.sms[0].y, chip.sms[0].stalled), (5, True), "PULL waits")
        chip.step()
        self.assertEqual((chip.sms[0].osr, chip.sms[0].stalled), (0xABCD, False), "clock 6")
        load(chip, 1, assemble("set x, 9\nmov isr, x\npush\nh: jmp h"))
        chip.reg_write(0, CTRL, 0b10)
        chip.step(4)
        self.assertEqual(chip.host(OP_RX_POP, 1), 0, "pushed in clock 3, in OUT at the end of 5")
        self.assertEqual(chip.host(OP_RX_POP, 1), 9)

    def test_clkdiv_16_8(self):
        chip = Chip(2)
        load(chip, 0, assemble("loop:\nin null, 1\njmp loop"), clkdiv_int=651, clkdiv_frac=128)
        self.assertEqual((chip.reg_read(0, CLKDIV), chip.reg_read(0, CLKDIV_FRAC)), (651, 128))
        chip.reg_write(0, CTRL, 1)
        strobes = 0
        for _ in range(6520):
            chip.step()
            strobes += chip.sms[0].strobed
        self.assertEqual(strobes, 10, "one strobe every 651.5 clocks (9600 baud x 8)")
        chip.reg_write(0, CLKDIV, 0)
        self.assertEqual(chip.reg_read(0, CLKDIV), 1, "0 = 1")

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

    def test_peer_irq(self):
        chip = Chip(2)
        load(chip, 0, assemble("irq peer 2         ; flag 2 of SM 1\nend: jmp end"))
        load(chip, 1, assemble("wait 1 irq 2       ; its own flag, set by SM 0\nirq 0\nend: jmp end"))
        chip.reg_write(0, CTRL, 0b11)
        chip.step(20)
        self.assertTrue(chip.sms[1].irq_flags[0])
        self.assertEqual(chip.reg_read(0, IRQ_SUM), 0b10)      # only SM 1

    def test_wait_on_a_peer_flag(self):
        chip = Chip(2)
        load(chip, 0, assemble("wait 1 peer_irq 5\nirq 0\nend: jmp end"))
        chip.reg_write(0, CTRL, 0b01)
        chip.step(5)
        self.assertEqual(chip.reg_read(0, PC), 0, "waiting")
        chip.reg_write(1, IRQ, 1 << 13)            # host sets flag 5 of SM 1
        chip.step(3)
        self.assertTrue(chip.sms[0].irq_flags[0])

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
            chip.reg_write(0, CTRL, 1 << 8)        # STEP: one strobe from the next clock
            chip.step(3)
        self.assertEqual(chip.reg_read(0, PC), 1)
        chip.reg_write(0, CTRL, 1 << 8)
        chip.step(3)
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

    def test_pads_of_the_tiny_tapeout_chip(self):
        """Pins 0..7 on uio; pin 8 + k on uo_out[k] / ui_in[k]: outputs 10..15, inputs 11..15."""
        chip = Chip(2)
        self.assertEqual([chip.reg_read(0, r) for r in (PADS_OUT, PADS_IN, PADS_BIDIR)],
                         [0xFCFF, 0xF8FF, 0x00FF])
        load(chip, 0, assemble("mov pins, x\nend: jmp end"), x=0xFFFF)
        chip.reg_write(0, GPIO, 0xFFFF)                          # open drain everywhere
        chip.reg_write(0, CTRL, 1)
        chip.step()
        out, oe = chip.pins
        self.assertEqual((out, oe), (0xFC00, 0xFC00),
                         "uio: latch 1 in open drain floats; pins 10..15 always drive, 8 and 9 have no pad")
        self.assertEqual((chip.reg_read(0, PINS), chip.reg_read(0, PINS_OE)), (out, oe))
        self.assertEqual(chip.uo_out, 0xFC)
        chip.pin_in = 0xFFFF
        chip.step(2)                                             # input synchronizer
        self.assertEqual(chip.reg_read(0, PINS_IN), 0xF8FF, "pins 8..10: no input pad, read 0")

    def test_host_refuses_open_drain_without_a_bidirectional_pad(self):
        with self.assertRaises(ValueError):
            load(Chip(2), 0, assemble(".open_drain 11\nend: jmp end"))
        load(Chip(2, pads=(0xFFFF,) * 3), 0, assemble(".open_drain 11\nend: jmp end"))   # an FPGA

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
