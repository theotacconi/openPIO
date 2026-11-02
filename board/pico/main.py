"""main.py - USB-to-SPI bridge for the openPIO host interface, MicroPython on a Raspberry Pi
Pico (RP2040 or RP2350). The PC drives the chip through it with sim/model/host.py: see
board/board.py.

One text line in, one line out, over the Pico's USB serial port:

    ?                 -> openpio-bridge 1
    <hex words>       -> one SPI transaction (CS_N low, the words, CS_N high): the MISO words
    f <hz>            -> SCLK frequency (default 1 MHz; HOST.md: at most clk / 6)
    i                 -> HOST_IRQ, 0 or 1
    anything else     -> e <reason>

Words are 16-bit hex, MSB first, mode 0 (HOST.md, SPI). The lines hold only hex digits,
letters and spaces, never Ctrl-C, so mpremote can still stop the program.
"""

import sys

SCK, MOSI, MISO, CS_N, IRQ = 2, 3, 4, 5, 6      # GPIO numbers: SPI0 and two free pins
HZ = 1_000_000


def reply(line, xfer, irq, clock):
    """The answer to one command line, without its newline. xfer(bytes) -> bytes is one SPI
    transaction; irq() -> 0/1; clock(hz) sets SCLK."""
    f = line.split()
    try:
        if not f:
            return "e empty"
        if f[0] == "?":
            return "openpio-bridge 1"
        if f[0] == "i":
            return str(irq())
        if f[0] == "f":
            clock(int(f[1]))
            return f[1]
        out = bytearray()
        for w in f:
            v = int(w, 16)
            if not 0 <= v <= 0xFFFF:
                return "e word " + w
            out.append(v >> 8)
            out.append(v & 0xFF)
        got = xfer(out)
        return " ".join("%04x" % (got[i] << 8 | got[i + 1]) for i in range(0, len(got), 2))
    except (ValueError, IndexError):
        return "e " + line.strip()


def main():
    from machine import SPI, Pin

    spi = SPI(0, baudrate=HZ, polarity=0, phase=0, bits=8, firstbit=SPI.MSB,
              sck=Pin(SCK), mosi=Pin(MOSI), miso=Pin(MISO))
    cs = Pin(CS_N, Pin.OUT, value=1)
    irq = Pin(IRQ, Pin.IN, Pin.PULL_DOWN)

    def xfer(out):
        got = bytearray(len(out))
        cs(0)
        spi.write_readinto(out, got)
        cs(1)
        return got

    def clock(hz):
        spi.init(baudrate=hz)

    while True:
        line = sys.stdin.readline()
        sys.stdout.write(reply(line, xfer, irq.value, clock) + "\n")


if sys.implementation.name == "micropython":   # imported by the PC's tests, run on the Pico
    main()
