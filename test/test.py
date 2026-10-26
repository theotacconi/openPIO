"""cocotb tests of the whole chip, through its pins only.

The test plays the host microcontroller: it talks to the chip over the SPI
slave on ui_in[2:0] (protocol in src/spi_cfg_interface.v) and drives / watches
the PIO pins uio[7:0]. Only top-level pins are used, so the same tests run on
the RTL and on the hardened gate-level netlist (make GATES=yes).

    test_registers      reset values and write/read-back of the config registers
    test_fifo_echo      TX FIFO -> SM -> RX FIFO: order, full flags, dropped push
    test_uart_rx        serial bytes on uio[0] -> agents/uart program -> RX FIFO, IRQ pin
    test_usb_vs_emu     agents/usb_ls (PAU NRZI + bit-stuffing) pins, clock by clock vs emu.py
    test_nrzi16_vs_emu  same, 16 bits per OUT
"""
import random
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sim" / "model"))
from asm import assemble          # noqa: E402  golden assembler
from emu import StateMachine      # noqa: E402  golden model

# SPI ops: cmd = {op[2:0], addr[4:0]}, then 16 data bits
OP_IMEM_WR, OP_REG_WR, OP_REG_RD, OP_TX_PUSH, OP_RX_POP, OP_IRQ = 1, 2, 3, 4, 5, 6
CTRL, WRAP, SIDESET, PINBASE, MISC, CLKDIV, GPIO, STATUS = 0, 1, 2, 3, 4, 5, 6, 8
EN, RESET = 0b01, 0b10                       # CTRL bits
TX_EMPTY, TX_FULL, RX_EMPTY, RX_FULL = 1, 2, 4, 8   # STATUS bits
# Writable bits and reset value of registers 0..6
REG_MASK = {0: 0x0003, 1: 0x1F1F, 2: 0x071F, 3: 0x7777, 4: 0x0037, 5: 0xFF1F, 6: 0xF8FF}
REG_RESET = {0: 0x0000, 1: 0x1F00, 2: 0x0000, 3: 0x5000, 4: 0x0030, 5: 0x0001, 6: 0x0000}

HALF = 6          # SCK half period in clk cycles: the chip needs >= 6 (f_SCK <= f_clk / 12)


# --------------------------------------------------------------------------- host side

async def start(dut):
    """Start the 50 MHz clock and reset the chip; SPI idle (CS_N high), PIO pins low."""
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    dut.ena.value = 1
    dut.ui_in.value = 0b100
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)


async def spi(dut, op, addr=0, data=0):
    """One 24-bit SPI frame (mode 0, MSB first); returns the 16-bit answer from MISO."""
    word = (op << 21) | (addr << 16) | data
    dut.ui_in.value = 0b000                        # CS_N low
    await ClockCycles(dut.clk, HALF)
    answer = 0
    for i in range(23, -1, -1):
        mosi = (word >> i) & 1
        dut.ui_in.value = mosi << 1                # SCK low, MOSI = next bit
        await ClockCycles(dut.clk, HALF)
        answer = (answer << 1) | int(dut.uo_out.value[0])   # MISO, read before SCK rises
        dut.ui_in.value = (mosi << 1) | 1          # SCK high: the chip samples MOSI
        await ClockCycles(dut.clk, HALF)
    dut.ui_in.value = 0b000
    await ClockCycles(dut.clk, HALF)
    dut.ui_in.value = 0b100                        # CS_N high: end of frame
    await ClockCycles(dut.clk, HALF)
    return answer & 0xFFFF                         # bits during the command byte are 0


async def load(dut, sm, gpio=0):
    """Program IMEM and the config registers from an emu.py StateMachine, then RESET.
    Leaves the SM stopped (EN = 0)."""
    for addr, word in enumerate(sm.memory):
        await spi(dut, OP_IMEM_WR, addr, word)
    await spi(dut, OP_REG_WR, WRAP, sm.wrap_top << 8 | sm.wrap_bottom)
    await spi(dut, OP_REG_WR, SIDESET, sm.sideset_base << 8 | sm.side_set_pindirs << 4
              | sm.side_set_opt << 3 | sm.side_set_count)
    await spi(dut, OP_REG_WR, PINBASE, sm.set_count << 12 | sm.set_base << 8
              | sm.out_base << 4 | sm.in_base)
    await spi(dut, OP_REG_WR, MISC, sm.shift_out_right << 5 | sm.shift_in_right << 4 | sm.jmp_pin)
    await spi(dut, OP_REG_WR, CLKDIV, sm.clkdiv_frac << 8 | sm.clkdiv_int)
    await spi(dut, OP_REG_WR, GPIO, gpio)
    await spi(dut, OP_REG_WR, CTRL, RESET)         # RESET: SM to PC 0, FIFOs emptied
    await spi(dut, OP_REG_WR, CTRL, 0)


def agent(name, **kw):
    """emu.py StateMachine loaded with agents/<name>/<name>.asm."""
    return StateMachine(assemble((ROOT / "agents" / name / f"{name}.asm").read_text()), **kw)


# --------------------------------------------------------------------------- tests

@cocotb.test()
async def test_registers(dut):
    """Reset values, then random values written and read back (only writable bits stick)."""
    await start(dut)
    expect = dict(REG_RESET)
    expect[STATUS] = TX_EMPTY | RX_EMPTY           # SM stopped at PC 0, FIFOs empty
    for addr in range(16):                         # 7, 9..15: 0 after reset
        got = await spi(dut, OP_REG_RD, addr)
        assert got == expect.get(addr, 0), f"reg {addr} after reset: {got:#06x}"

    rng = random.Random(1)
    for _ in range(2):
        for addr in range(1, 7):                   # not CTRL: that would start the SM
            val = rng.randrange(1 << 16)
            await spi(dut, OP_REG_WR, addr, val)
            got = await spi(dut, OP_REG_RD, addr)
            assert got == val & REG_MASK[addr], f"reg {addr}: wrote {val:#06x}, read {got:#06x}"


@cocotb.test()
async def test_fifo_echo(dut):
    """The SM copies every TX word to RX: words come back in order, the 5th push
    into a full TX FIFO is dropped, status flags follow."""
    await start(dut)
    await load(dut, StateMachine(assemble("""
    .wrap_target
        pull block
        mov isr, osr
        push block
    .wrap
    """)))
    words = [0x1234, 0xABCD, 0x0000, 0xFFFF]
    for w in words:                                # SM stopped: TX fills up
        await spi(dut, OP_TX_PUSH, data=w)
    await spi(dut, OP_TX_PUSH, data=0xDEAD)        # TX full: dropped
    status = await spi(dut, OP_REG_RD, STATUS)
    assert status & 0xF == TX_FULL | RX_EMPTY, f"status with TX full: {status:#06x}"

    await spi(dut, OP_REG_WR, CTRL, EN)
    await ClockCycles(dut.clk, 50)                 # 4 words x 3 instructions
    status = await spi(dut, OP_REG_RD, STATUS)
    assert status & 0xF == TX_EMPTY | RX_FULL, f"status after echo: {status:#06x}"
    got = [await spi(dut, OP_RX_POP) for _ in words]
    assert got == words, f"echo: sent {[hex(w) for w in words]}, got {[hex(w) for w in got]}"
    assert await spi(dut, OP_RX_POP) == 0, "pop from empty RX must answer 0"


@cocotb.test()
async def test_uart_rx(dut):
    """UART receiver program (8 clk per bit, pin uio[0]): valid bytes land in RX,
    a frame with a low stop bit raises IRQ 0 instead (HOST_IRQ = uo_out[1])."""
    await start(dut)
    await load(dut, agent("uart", in_base=0, jmp_pin=0))
    dut.uio_in.value = 1                           # line idle high
    await spi(dut, OP_REG_WR, CTRL, EN)

    async def send(byte, stop=1):
        # start bit, 8 data bits LSB first, stop bit held 2 bit times: uart.asm
        # samples the stop bit 12 clocks into it, not in the middle
        for bit in [0] + [(byte >> i) & 1 for i in range(8)] + [stop, stop]:
            dut.uio_in.value = bit
            await ClockCycles(dut.clk, 8)
        dut.uio_in.value = 1
        await ClockCycles(dut.clk, 16)

    data = [0xA5, 0x00, 0xFF, 0x3C]
    for b in data:
        await send(b)
    got = [await spi(dut, OP_RX_POP) >> 8 for _ in data]   # in shift right: byte in [15:8]
    assert got == data, f"UART RX: sent {[hex(b) for b in data]}, got {[hex(b) for b in got]}"

    assert int(dut.uo_out.value[1]) == 0, "IRQ pin high before any framing error"
    await send(0x55, stop=0)
    await ClockCycles(dut.clk, 8)
    assert int(dut.uo_out.value[1]) == 1, "framing error did not raise the IRQ pin"
    assert await spi(dut, OP_IRQ, data=0x0001) & 0xFF == 0x01, "IRQ flags: only IRQ 0 expected"
    assert int(dut.uo_out.value[1]) == 0, "IRQ 0 not cleared by the IRQ command"
    assert await spi(dut, OP_REG_RD, STATUS) & RX_EMPTY, "bad frame must not reach RX"


async def pins_vs_emu(dut, sm, words, clocks):
    """Load sm's program, push words into the TX FIFO, start the SM: the pins
    (uio_out, uio_oe) must equal emu.py on every clock from the SM start."""
    await load(dut, sm)
    for w in words:                                # at most 4: the TX FIFO depth
        await spi(dut, OP_TX_PUSH, data=w)
        sm.tx_fifo.append(w)

    # The SM starts inside the EN frame, so recording starts before it
    chip = []
    async def record():                            # runs in parallel, one sample per clock
        while True:
            chip.append((int(dut.uio_out.value), int(dut.uio_oe.value)))
            await ClockCycles(dut.clk, 1)
    recorder = cocotb.start_soon(record())
    await spi(dut, OP_REG_WR, CTRL, EN)
    await ClockCycles(dut.clk, clocks)
    recorder.cancel()
    model = [(sm.gpio_out, sm.pindirs)]            # pins before the first step
    for _ in range(clocks - 20):
        sm.step()
        model.append((sm.gpio_out, sm.pindirs))

    # The chip trace starts with idle pins (= model[0]) while the EN frame is sent:
    # find where the SM starts, then everything after must match exactly.
    shifts = range(len(chip) - len(model) + 1)
    if not any(chip[k:k + len(model)] == model for k in shifts):
        k = max(shifts, key=lambda k: sum(a == b for a, b in zip(chip[k:], model)))
        i = next(i for i, (a, b) in enumerate(zip(chip[k:], model)) if a != b)
        assert False, f"pins differ from emu.py at SM step {i}: chip {chip[k + i]}, emu {model[i]}"
    dut._log.info(f"pins match emu.py for {len(model)} clocks")


@cocotb.test()
async def test_usb_vs_emu(dut):
    """agents/usb_ls packets on uio[1:0]: PAU NRZI encoding and bit-stuffing,
    one bit per OUT (stuffing decided by the ones counter carried across OUTs)."""
    await start(dut)
    sm = agent("usb_ls", set_base=0, out_base=0)
    # sync 0x80 + data byte; 0xFF and 0x7E need a stuffed bit
    await pins_vs_emu(dut, sm, [0xFF80, 0xAA80, 0x0080, 0x7E80], 400)
    assert sm.irq_flags[0] and int(dut.uo_out.value[1]) == 1, "packet-done IRQ 0"


@cocotb.test()
async def test_nrzi16_vs_emu(dut):
    """NRZI + bit-stuffing over 16 bits per OUT. Only the line state after each
    OUT reaches the pins, so the words make a misplaced stuffed bit change the
    number of toggles (bits sent LSB first):
      0xFFFF  16 ones: stuff after the 6th and 12th
      0xFC3F  six 1s, four 0s, six 1s: stuff at the end of both runs
      0x0F3E  0, five 1s, ...: five 1s must NOT stuff
      0x7FFE  0, fourteen 1s, 0: stuff after the 6th and 12th"""
    await start(dut)
    sm = StateMachine(assemble("""
        set pau_cfg, 3      ; NRZI + bit-stuffing
        set pins, 1         ; line to J
    .wrap_target
        pull block
        out pins, 16 [1]
    .wrap
    """), set_base=0, out_base=0)
    await pins_vs_emu(dut, sm, [0xFFFF, 0xFC3F, 0x0F3E, 0x7FFE], 80)
