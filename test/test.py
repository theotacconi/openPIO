"""cocotb tests of the whole chip, through its pins only (HOST.md), so the same tests run on the
RTL and on the hardened gate-level netlist (make GATES=yes).

The test is the host microcontroller: SPI on ui_in[2:0] / uo_out[0] at SCLK = clk / 6, the
transactions sim/model/host.py makes to load a program (recorded, then played on the pins),
and the reference devices of test/shared/devices.py on the PIO pins. test/lockstep/ checks the
same RTL against the model clock by clock; these tests check the chip as a host and a board
see it.

    test_id_and_registers   ID, reset values, write and read back, the header's MISO
    test_fifo_echo          TX FIFO -> SM -> RX FIFO through the SRAM rings, RX_POP's count,
                            at three splits of the memory (FIFO_SPLIT 16, 1, 31)
    test_uart_tx            programs/uart/uart_tx.asm judged by the reference UART receiver
    test_uart_rx            programs/uart/uart_rx.asm fed by the reference UART transmitter
    test_alu_and_timing     every ALU operation, IN TIME, DLYMUL, a timed WAIT, a condition
                            the program writes (ALU dest COND)
    test_pins_10_to_15      side-set and SET on pins 11-15, read back through ui_in by the
                            other SM, woken by a peer IRQ; a flag on HOST_IRQ
    test_loopback_*         board/loopback.py's checks, unchanged: a transmitter on SM 0, the
                            receiver on SM 1 through the chip's pads (the PAU's line codes,
                            stuffing, CRCs and monitor, open drain, FIFO_SPLIT, IN TIME)
"""
import sys
from pathlib import Path

import cocotb
from cocotb._bridge import bridge, resume       # cocotb 2.0.1's names for them (public in 2.1)
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "sim" / "model"), str(ROOT / "test" / "shared"), str(ROOT / "board")]
from asm import assemble                                       # noqa: E402
from chip import (CTRL, ID, OP_IMEM_RD, OP_REG_RD, OP_REG_WR, OP_RX_POP, OP_TX_PUSH, PADS_BIDIR,  # noqa: E402
                  PC, STATUS, WRAP, Chip, header, sm_register)
from devices import UartRx, UartTx                             # noqa: E402
from host import Host                                          # noqa: E402
import loopback                                                # noqa: E402
from seeds import seeded                                 # noqa: E402

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
        self.ui_loop = False                        # uo_out[7:3] wired to ui_in[7:3]

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
            loop = int(dut.uo_out.value) & 0xF8 if self.ui_loop else 0
            dut.ui_in.value = loop | self.spi_pins
            self.t += 1

    async def spi(self, hdr: int, words=(), lead: int = HALF, idle: int = 2 * HALF) -> list[int]:
        """One transaction, mode 0, MSB first: the MISO words (header first). CS_N falls
        lead + HALF clocks before the first SCLK edge and stays high idle clocks after."""
        miso = []
        self.spi_pins = 0b000                       # CS_N low
        await ClockCycles(self.dut.clk, lead)
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
        await ClockCycles(self.dut.clk, idle)
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
    assert await board.read(0, ID) == [0x3250], "ISA v3, 2 SMs, 'P'"
    model = Chip(2)
    for sm in range(2):
        got = await board.read(sm, 1, 31)                     # registers 0x01..0x1F
        want = [sm_register(model.sms[sm], a, 0xFF) for a in range(1, 32)]
        assert got == want, f"SM {sm} after reset: {[hex(v) for v in got]}"
    await board.load(1, "loop:\njmp loop")                      # SM 1 runs only words written
    await board.write(1, WRAP, 0x2A05, 0x0F1B)                 # WRAP, SIDESET
    assert await board.read(1, WRAP, 2) == [0x2A05, 0x0F1B]
    assert await board.read(0, WRAP) == [0xFF00], "SM 0 untouched"
    await board.write(0, CTRL, 0b10)                           # SM 1 runs: STATUS bit 5
    # the header's MISO is the STATUS of the previous transaction's SM
    assert (await board.spi(header(OP_REG_RD, 1, PC), [0]))[0] >> 5 & 1 == 0, "SM 0's"
    assert (await board.spi(header(OP_REG_RD, 1, STATUS), [0]))[0] >> 5 & 1 == 1, "SM 1's"


@cocotb.test()
async def test_cs_n_timing(dut):
    """HOST.md's CS_N limits: CS_N falls 5 clocks before the first SCLK edge and the header's
    MISO is already STATUS (its MSB first); CS_N high for 2 clocks between transactions."""
    board = Board(dut)
    await board.start()
    await board.write(0, PC, 0xA5)                             # STATUS[15:8] = PC: MSB 1
    want = (await board.read(0, STATUS))[0]
    assert want >> 8 == 0xA5
    for _ in range(3):
        await board.spi(header(OP_RX_POP, 0, 1), [0, 0], idle=2)   # MISO ends at 0
        got = await board.spi(header(OP_REG_RD, 0, STATUS), [0], lead=5 - HALF, idle=2)
        assert got == [want, want], [hex(v) for v in got]
        await board.write(0, PC, 0x3C)                         # a write, then a read 2 clocks on
        await board.spi(header(OP_REG_WR, 0, PC), [0xA5], idle=2)
        assert await board.read(0, PC) == [0xA5]


@cocotb.test()
async def test_pages(dut):
    """Two programs in pages 0 and 3 of SM 0's IMEM: JMP and OUT PC stay in page 3, then the
    host switches to page 0 without reloading it (ISA.md, Pages)."""
    board = Board(dut)
    await board.start()
    table = (".y 1\n.start start\njmp even\njmp odd\nstart:\npull block\nmov x, osr\n"
             "out pc, 1\neven:\nadd x, y\nodd:\nadd x, y\nmov isr, x\npush block\njmp start")
    await board.load(0, ".wrap_target\npull block\nmov isr, osr\npush block\n.wrap")
    await board.load(0, table, page=3)
    assert (await board.spi(header(OP_IMEM_RD, 0), [0xC0, 0, 0]))[2:] == list(assemble(table))[:2]
    await board.write(0, CTRL, 0b01)
    await board.spi(header(OP_TX_PUSH, 0), [4, 5, 6, 7])
    await ClockCycles(dut.clk, 300)
    assert (await board.spi(header(OP_RX_POP, 0, 4), [0] * 4))[1:] == [6, 6, 8, 8], "page 3"
    assert (await board.read(0, PC))[0] >> 6 == 3, "PC stayed in page 3"
    await board.write(0, CTRL, 1 << 4)                         # stop, RESET
    await board.write(0, WRAP, 0x0200)                         # the echo's wrap, PC 0
    await board.write(0, CTRL, 0b01)
    await board.spi(header(OP_TX_PUSH, 0), [4, 5])
    await ClockCycles(dut.clk, 200)
    assert (await board.spi(header(OP_RX_POP, 0, 2), [0] * 2))[1:] == [4, 5], "page 0: the echo"


@cocotb.test()
@cocotb.parametrize(split=[0, 1, 31])
async def test_fifo_echo(dut, split):
    """150 words through an echo program: the SRAM rings fill (128 words each, or TX 8 and RX
    248, or TX 248 and RX 8); RX_POP of a count."""
    board = Board(dut)
    await board.start()
    await board.load(0, "loop:\npull block\nmov isr, osr\npush block\njmp loop", fifo_split=split)
    await board.write(0, CTRL, 0b01)
    words = seeded(1).choices(range(1 << 16), k=150)
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
    data = seeded(2).choices(range(256), k=12)
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
    data = seeded(3).choices(range(256), k=12)
    tx = UartTx(3, 32, data, start=board.t + 20)
    board.devices.append(tx)
    await ClockCycles(dut.clk, tx.end - board.t + 100)
    got = (await board.spi(header(OP_RX_POP, 0, len(data)), [0] * len(data)))[1:]
    assert [w >> 8 for w in got] == data
    assert int(dut.uo_out.value) >> 1 & 1 == 0, "no framing error: HOST_IRQ low"


ALU_AND_TIMING = """
.x 0x1234
.y 0x00F0
    mov isr, x
    push
    rev isr, x
    push
    mov isr, x
    and isr, y
    push
    mov isr, x
    or isr, y
    push
    mov isr, x
    xor isr, y
    push
    mov isr, x
    xnor isr, y
    push
    mov isr, x
    add isr, y
    push
    mov isr, x
    sub isr, y
    push
    in time, 16             ; strobe 22
    push
    set dlymul, 3
    nop [4]                 ; 1 + 4 x 3 strobes
    in time, 16             ; 22 + 16
    push
    set timeout_val, 5      ; 5 x 3 strobes
    wait 1 irq 7 timeout    ; never set: issued at 41, ends 15 strobes later
    in time, 16             ; 57
    push
    jmp timeout, late
    set x, 1
late:
    pull block
    mov cond, osr           ; slot 1 = the word the host sent
    set y, 1
    jmp cond1, one
    set x, 2
one:
    mov isr, x
    push
    set y, 0
    jmp cond1, wrong
    set x, 3
wrong:
    mov isr, x
    push
halt:
    jmp halt
"""


@cocotb.test()
async def test_alu_and_timing(dut):
    """What the PAU loopbacks do not reach: the eight ALU operations on X = 0x1234 and Y =
    0x00F0, IN TIME across a delay times DLYMUL and a timed WAIT, the timeout flag, and a
    condition the program writes (slot 1 = bit 0 of Y, sent by the host: 0x0150)."""
    board = Board(dut)
    await board.start()
    await board.load(0, ALU_AND_TIMING)
    await board.write(0, CTRL, 0b01)
    await board.spi(header(OP_TX_PUSH, 0), [0x0150])
    await ClockCycles(dut.clk, 400)
    got = (await board.spi(header(OP_RX_POP, 0, 13), [0] * 13))[1:]
    assert got == [0x1234, 0x2C48, 0x0030, 0x12F4, 0x12C4, 0xED3B, 0x1324, 0x1144,
                   22, 38, 57, 0x1234, 3], [hex(w) for w in got]


PINS_OUT = """
.sideset_pins 11
.side_set 1 opt
.set_pins 12 4
    set pins, 0b1010 side 1     ; pins 15..11 = 1, 0, 1, 0, 1
    irq peer 2                  ; wake SM 1
halt:
    jmp halt
"""

PINS_IN = """
.in_pins 11
.shift_in left
    wait 1 irq 2 [7]            ; the wire out and in, and the synchronizer: a few clocks
    in pins, 5                  ; pins 11..15, through ui_in[3..7]
    push
    irq 0                       ; HOST_IRQ
halt:
    jmp halt
"""


@cocotb.test()
async def test_pins_10_to_15(dut):
    """Pins 11-15 are uo_out[3..7] and ui_in[3..7]: wired out to in here. SM 0 sets them by
    side-set and SET and wakes SM 1 with a peer IRQ; SM 1 reads them and raises HOST_IRQ."""
    board = Board(dut)
    board.ui_loop = True
    await board.start()
    await board.load(0, PINS_OUT)
    await board.load(1, PINS_IN)
    assert int(dut.uo_out.value) >> 1 & 1 == 0, "HOST_IRQ low"
    await board.write(0, CTRL, 0b11)
    await ClockCycles(dut.clk, 40)
    assert int(dut.uo_out.value) >> 3 == 0b10101, f"uo_out {int(dut.uo_out.value):#04x}"
    assert (await board.spi(header(OP_RX_POP, 1, 1), [0]))[1:] == [0b10101]
    assert int(dut.uo_out.value) >> 1 & 1 == 1, "SM 1's flag 0 on HOST_IRQ"


class Blocking:
    """Board.spi for code that blocks (host.Host, board/loopback.py), run in a thread by
    bridge(): each call waits for the transaction on the pins."""

    def __init__(self, board: Board) -> None:
        self._spi = resume(board.spi)

    def spi(self, hdr: int, words=()) -> list[int]:
        return self._spi(hdr, list(words))


@cocotb.test()
@cocotb.parametrize(check=loopback.CHECKS)
async def test_loopback(dut, check):
    """A check of board/loopback.py on the pins, as on the FPGA board: pins 0-7 pulled up when
    nothing drives them (tang_top.v), the host at SCLK = clk / 6."""
    board = Board(dut)
    board.pullups = 0xFF
    await board.start()
    error = await bridge(check)(Host(Blocking(board)))
    assert error is None, f"{check.__name__}: {error}"
