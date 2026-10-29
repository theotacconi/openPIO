"""cocotb tests of the whole chip, through its pins only (HOST.md), so the same tests run on the
RTL and on the hardened gate-level netlist (make GATES=yes).

The test is the host microcontroller: SPI on ui_in[2:0] / uo_out[0] at SCLK = clk / 6, the
transactions sim/model/host.py makes to load a program (recorded, then played on the pins),
and the reference devices of test/shared/devices.py on the PIO pins. test/lockstep/ checks the
same RTL against the model clock by clock; these tests check the chip as a host and a board
see it.

    test_id_and_registers   ID, reset values, write and read back, the header's MISO
    test_fifo_echo          TX FIFO -> SM -> RX FIFO through the SRAM rings, RX_POP's count
    test_uart_tx            programs/uart/uart_tx.asm judged by the reference UART receiver
    test_uart_rx            programs/uart/uart_rx.asm fed by the reference UART transmitter
"""
import random
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "sim" / "model"), str(ROOT / "test" / "shared")]
from asm import assemble                                       # noqa: E402
from chip import (CTRL, ID, OP_REG_RD, OP_REG_WR, OP_RX_POP, OP_TX_PUSH, PADS_BIDIR,  # noqa: E402
                  PC, STATUS, WRAP, Chip, header, sm_register)
from devices import UartRx, UartTx                             # noqa: E402
from host import Host                                          # noqa: E402

HALF = 3                    # clocks of each SCLK phase: SCLK = clk / 6, HOST.md's limit


class Recorder:
    """A Chip for host.Host that records its SPI transactions (to play them on the pins) and
    answers its reads as a stopped chip after reset would."""

    def __init__(self) -> None:
        self.transactions: list[tuple[int, list[int]]] = []

    def spi(self, hdr: int, words=()) -> list[int]:
        self.transactions.append((hdr, list(words)))
        op, addr = hdr >> 13, hdr & 0x3F
        if op == OP_REG_RD:
            return [0] + [{PADS_BIDIR: 0x00FF}.get(addr + k, 0) for k in range(len(words))]
        return [0] * (len(words) + 1)


class Board:
    """The chip's pins: the SPI host on ui_in, devices on the 16 pin positions."""

    def __init__(self, dut) -> None:
        self.dut = dut
        self.devices = []
        self.pullups = 0
        self.t = 0
        self.spi_pins = 0b100                       # CS_N high

    async def start(self) -> None:
        dut = self.dut
        cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
        dut.ena.value = 1
        dut.ui_in.value = self.spi_pins
        dut.uio_in.value = 0
        dut.rst_n.value = 0
        await ClockCycles(dut.clk, 10)
        dut.rst_n.value = 1
        cocotb.start_soon(self._pins())

    async def _pins(self) -> None:
        """Every clock: resolve pins 0..7 (chip, devices, pull-ups), let the devices see them."""
        dut = self.dut
        while True:
            await RisingEdge(dut.clk)
            out, oe = int(dut.uio_out.value), int(dut.uio_oe.value)
            low = high = 0
            for d in self.devices:
                for pin, lv in d.drive.items():
                    if lv:
                        high |= 1 << pin
                    else:
                        low |= 1 << pin
            low |= oe & ~out
            high |= oe & out
            levels = ~low & (high | self.pullups) & 0xFF
            for d in self.devices:
                d.tick(self.t, levels)
            dut.uio_in.value = levels
            dut.ui_in.value = self.spi_pins
            self.t += 1

    async def spi(self, hdr: int, words=()) -> list[int]:
        """One transaction, mode 0, MSB first: the MISO words (header first)."""
        miso = []
        self.spi_pins = 0b000                       # CS_N low
        await ClockCycles(self.dut.clk, HALF)
        for word in [hdr, *words]:
            got = 0
            for bit in range(15, -1, -1):
                mosi = word >> bit & 1
                self.spi_pins = mosi << 1
                await ClockCycles(self.dut.clk, HALF)
                got = got << 1 | int(self.dut.uo_out.value) & 1
                self.spi_pins = mosi << 1 | 1       # SCLK rises: both sides sample
                await ClockCycles(self.dut.clk, HALF)
            miso.append(got)
        self.spi_pins = 0b100
        await ClockCycles(self.dut.clk, 2 * HALF)
        return miso

    async def read(self, sm: int, addr: int, count: int = 1) -> list[int]:
        return (await self.spi(header(OP_REG_RD, sm, addr), [0] * count))[1:]

    async def write(self, sm: int, addr: int, *words: int) -> None:
        await self.spi(header(OP_REG_WR, sm, addr), words)

    async def load(self, sm: int, source: str, **cfg) -> None:
        """What host.Host.load does: IMEM, registers, RESET, PC / X / Y; the SM stays stopped."""
        rec = Recorder()
        Host(rec).load(sm, assemble(source), **cfg)
        for hdr, words in rec.transactions:
            await self.spi(hdr, words)


@cocotb.test()
async def test_id_and_registers(dut):
    board = Board(dut)
    await board.start()
    assert await board.read(0, ID) == [0x2250], "ISA v2, 2 SMs, 'P'"
    model = Chip(2)
    for sm in range(2):
        got = await board.read(sm, 1, 31)                     # registers 0x01..0x1F
        want = [sm_register(model.sms[sm], a, 0xFF) for a in range(1, 32)]
        assert got == want, f"SM {sm} after reset: {[hex(v) for v in got]}"
    await board.load(1, "loop:\njmp loop")                      # SM 1 runs only words written
    await board.write(1, WRAP, 0x2A05, 0x0F1B)                 # WRAP, SIDESET
    assert await board.read(1, WRAP, 2) == [0x2A05, 0x0F1B]
    assert await board.read(0, WRAP) == [0x3F00], "SM 0 untouched"
    await board.write(0, CTRL, 0b10)                           # SM 1 runs: STATUS bit 5
    # the header's MISO is the STATUS of the previous transaction's SM
    assert (await board.spi(header(OP_REG_RD, 1, PC), [0]))[0] >> 5 & 1 == 0, "SM 0's"
    assert (await board.spi(header(OP_REG_RD, 1, STATUS), [0]))[0] >> 5 & 1 == 1, "SM 1's"


@cocotb.test()
async def test_fifo_echo(dut):
    """150 words through an echo program: both 128-word SRAM rings fill; RX_POP of a count."""
    board = Board(dut)
    await board.start()
    await board.load(0, "loop:\npull block\nmov isr, osr\npush block\njmp loop")
    await board.write(0, CTRL, 0b01)
    words = [random.Random(1).randrange(1 << 16) for _ in range(150)]
    for k in range(0, len(words), 30):
        await board.spi(header(OP_TX_PUSH, 0), words[k:k + 30])
    got = []
    while len(got) < len(words):
        k = min(64, len(words) - len(got))
        got += (await board.spi(header(OP_RX_POP, 0, k & 63), [0] * k))[1:]
    assert got == words
    await board.spi(header(OP_TX_PUSH, 0), [0xBEEF, 0xCAFE])
    await ClockCycles(dut.clk, 50)
    assert (await board.spi(header(OP_RX_POP, 0, 1), [0, 0, 0]))[1:] == [0xBEEF, 0, 0], \
        "one pop: the words beyond it read 0"
    assert (await board.spi(header(OP_RX_POP, 0, 1), [0]))[1:] == [0xCAFE], "none lost"


@cocotb.test()
async def test_uart_tx(dut):
    board = Board(dut)
    board.pullups = 1
    await board.start()
    rx = UartRx(0, 24)                                         # CLKDIV 3, 8 strobes a bit
    board.devices.append(rx)
    await board.load(0, (ROOT / "programs/uart/uart_tx.asm").read_text(), clkdiv_int=3)
    data = [random.Random(2).randrange(256) for _ in range(12)]
    await board.spi(header(OP_TX_PUSH, 0), data)
    await board.write(0, CTRL, 0b01)
    await ClockCycles(dut.clk, 24 * 10 * (len(data) + 2))
    assert rx.frames == data, rx.frames
    assert rx.errors == []


@cocotb.test()
async def test_uart_rx(dut):
    board = Board(dut)
    board.pullups = 1 << 3
    await board.start()
    await board.load(0, (ROOT / "programs/uart/uart_rx.asm").read_text(), clkdiv_int=4)
    await board.write(0, CTRL, 0b01)
    data = [random.Random(3).randrange(256) for _ in range(12)]
    tx = UartTx(3, 32, data, start=board.t + 20)
    board.devices.append(tx)
    await ClockCycles(dut.clk, tx.end - board.t + 100)
    got = (await board.spi(header(OP_RX_POP, 0, len(data)), [0] * len(data)))[1:]
    assert [w >> 8 for w in got] == data
    assert int(dut.uo_out.value) >> 1 & 1 == 0, "no framing error: HOST_IRQ low"
