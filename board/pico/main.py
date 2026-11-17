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

The Pico's own peripherals, the counterparts of the chip's programs in board/peripherals.py
(written by Raspberry Pi and MicroPython, not by this project); bytes as hex, no spaces; every
answer starts with r:

    pv                -> r <MicroPython version> i2ctarget|-
    u <baud>          -> r: UART1, 8N1, TX GP8, RX GP9 (its receive buffer emptied)
    ut <hex>          -> r: those bytes sent, once they are out
    ur                -> r <hex>: the bytes received since
    s <hz>            -> r <hz it runs at>: SPI1 controller, mode 0, MSB first, SCK GP10,
                         MOSI GP11, MISO GP12, CS_N GP13
    sx <hex>          -> r <hex>: one transfer, CS_N low for all of it: the MISO bytes
    t <addr>          -> r: I2C1 target at that 7-bit address (hex), SDA GP14, SCL GP15: a
                         memory of 256 bytes, all 0, at register addresses of one byte
    tw <reg> <hex>    -> r: those bytes into the memory from reg (hex)
    tm <reg> <n>      -> r <hex>: n bytes of the memory from reg
    g <mask> <values> -> r <levels>: GP8-GP15 (bit k: GP8 + k, hex) outputs where mask is 1, at
                         those values, the others inputs; the levels of all eight, read back
    x                 -> r: all three off, GP8-GP15 inputs again (as after a reset), so that
                         they do not drive the chip's pins in the other checks
"""

import sys

SCK, MOSI, MISO, CS_N, IRQ = 2, 3, 4, 5, 6      # GPIO numbers: SPI0 and two free pins
HZ = 1_000_000


def reply(line, xfer, irq, clock, more=None):
    """The answer to one command line, without its newline. xfer(bytes) -> bytes is one SPI
    transaction; irq() -> 0/1; clock(hz) sets SCLK; more: {command: f(arguments) -> answer},
    the peripherals' commands."""
    f = line.split()
    try:
        if not f:
            return "e empty"
        if more and f[0] in more:
            try:
                return more[f[0]](f[1:])
            except Exception as e:                  # a peripheral's error, not the bridge's
                return "e %s %s" % (f[0], e)
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


def peripherals():
    """The commands of the Pico's UART1, SPI1 and I2C1 (the module's docstring)."""
    import binascii
    from machine import SPI, UART, Pin
    try:
        from machine import I2CTarget                # MicroPython 1.26 and later
    except ImportError:
        I2CTarget = None
    on = {}

    def hexed(b):
        return "r " + binascii.hexlify(b).decode()

    def version(a):
        return "r %s %s" % (".".join(str(v) for v in sys.implementation.version[:3]),
                            "i2ctarget" if I2CTarget else "-")

    def uart(a):
        on["u"] = UART(1, baudrate=int(a[0]), bits=8, parity=None, stop=1, tx=Pin(8),
                       rx=Pin(9), rxbuf=4096, timeout=0)
        on["u"].read()
        return "r"

    def uart_send(a):
        on["u"].write(binascii.unhexlify(a[0]))
        on["u"].flush()
        return "r"

    def uart_read(a):
        return hexed(on["u"].read() or b"")

    def spi(a):
        on["s"] = SPI(1, baudrate=int(a[0]), polarity=0, phase=0, bits=8, firstbit=SPI.MSB,
                      sck=Pin(10), mosi=Pin(11), miso=Pin(12))
        on["cs"] = Pin(13, Pin.OUT, value=1)
        return "r " + str(on["s"]).split("baudrate=")[1].split(",")[0]

    def spi_transfer(a):
        out = binascii.unhexlify(a[0])
        got = bytearray(len(out))
        on["cs"](0)
        on["s"].write_readinto(out, got)
        on["cs"](1)
        return hexed(got)

    def target(a):
        if not I2CTarget:
            return "e t no machine.I2CTarget: MicroPython 1.26 or later"
        if "t" in on:
            on["t"].deinit()
        on["mem"] = bytearray(256)
        on["t"] = I2CTarget(1, int(a[0], 16), scl=Pin(15), sda=Pin(14), mem=on["mem"])
        return "r"

    def target_write(a):
        reg, data = int(a[0], 16), binascii.unhexlify(a[1])
        on["mem"][reg:reg + len(data)] = data
        return "r"

    def target_memory(a):
        reg = int(a[0], 16)
        return hexed(on["mem"][reg:reg + int(a[1])])

    def gpio(a):
        mask, values = int(a[0], 16), int(a[1], 16)
        pins = [Pin(8 + k, Pin.OUT, value=values >> k & 1) if mask >> k & 1 else
                Pin(8 + k, Pin.IN) for k in range(8)]
        return "r %02x" % sum(p.value() << k for k, p in enumerate(pins))

    def off(a):
        for k in ("u", "s", "t"):
            if k in on:
                on.pop(k).deinit()
        for n in range(8, 16):
            Pin(n, Pin.IN)
        return "r"

    return {"x": off, "g": gpio, "pv": version, "u": uart, "ut": uart_send, "ur": uart_read, "s": spi,
            "sx": spi_transfer, "t": target, "tw": target_write, "tm": target_memory}


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

    more = peripherals()
    while True:
        line = sys.stdin.readline()
        sys.stdout.write(reply(line, xfer, irq.value, clock, more) + "\n")


if sys.implementation.name == "micropython":   # imported by the PC's tests, run on the Pico
    main()
