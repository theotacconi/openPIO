"""Builds the wiring drawings of WIRING.md: wiring_bridge.svg (the Pico to the Tang Nano 20K)
and wiring_demo.svg (the IR receiver and the WS2812 ring).

    python3 fpga/tang_nano_20k/wiring.py

The Pico is drawn from the top, USB up, every pin in its place. The Tang Nano is a block with
the FPGA pin numbers, as they are printed next to its header pins.
"""
from html import escape
from pathlib import Path

HERE = Path(__file__).resolve().parent

STYLE = """
.bg { fill: #ffffff; }
text { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; fill: #1f2933; }
.title { font-size: 17px; font-weight: 700; }
.sub { font-size: 12px; fill: #52606d; }
.pin { font-size: 11.5px; fill: #9aa5b1; }
.pinu { font-size: 12px; font-weight: 700; fill: #1f2933; }
.num { font-size: 10.5px; fill: #9aa5b1; }
.numu { font-size: 10.5px; font-weight: 700; fill: #1f2933; }
.wire { font-size: 12.5px; font-weight: 700; }
.term { font-size: 12.5px; font-weight: 700; fill: #1f2933; }
.note { font-size: 12px; fill: #52606d; }
.warn { font-size: 12px; font-weight: 700; fill: #b42318; }
.pico { fill: #edf7ef; stroke: #2f8a4c; stroke-width: 2; }
.usb { fill: #d9dee4; stroke: #616e7c; stroke-width: 1.5; }
.tang { fill: #f7f8fa; stroke: #323f4b; stroke-width: 2.2; }
.part { fill: #ffffff; stroke: #616e7c; stroke-width: 1.8; }
.free { fill: #ffffff; stroke: #9aa5b1; stroke-width: 1.3; }
"""

# The Pico's pins, 1 to 40: 1-20 down the left side, 21-40 up the right side (top view, USB up).
PICO = (["GP0", "GP1", "GND", "GP2", "GP3", "GP4", "GP5", "GND", "GP6", "GP7", "GP8", "GP9",
         "GND", "GP10", "GP11", "GP12", "GP13", "GND", "GP14", "GP15"]
        + ["GP16", "GP17", "GND", "GP18", "GP19", "GP20", "GP21", "GND", "GP22", "RUN", "GP26",
           "GP27", "GND", "GP28", "ADC_VREF", "3V3(OUT)", "3V3_EN", "GND", "VSYS", "VBUS"])

ROW, PICO_W = 26, 230
PICO_H = 70 + 19 * ROW + 84


class Drawing:
    def __init__(self, w, h, title, desc):
        self.w, self.h, self.title, self.desc, self.p = w, h, title, desc, []

    def add(self, s):
        self.p.append(s)

    def text(self, x, y, s, cls, anchor="start", fill=None):
        f = f' style="fill:{fill}"' if fill else ""
        self.add(f'<text x="{x}" y="{y}" class="{cls}" text-anchor="{anchor}"{f}>'
                 f'{escape(s)}</text>')

    def line(self, pts, color, width=3):
        d = " ".join(f"{x},{y}" for x, y in pts)
        self.add(f'<polyline points="{d}" fill="none" stroke="{color}" stroke-width="{width}" '
                 f'stroke-linejoin="round" stroke-linecap="round"/>')

    def dot(self, x, y, color, r=4.5):
        self.add(f'<circle cx="{x}" cy="{y}" r="{r}" fill="{color}"/>')

    def arrow(self, x, y, color, left):
        s = -1 if left else 1
        self.add(f'<polygon points="{x + 7 * s},{y} {x - 6 * s},{y - 6} {x - 6 * s},{y + 6}" '
                 f'fill="{color}"/>')

    def save(self, name):
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" '
               f'width="{self.w}" height="{self.h}" role="img" aria-labelledby="t d">\n'
               f'<title id="t">{escape(self.title)}</title>\n<desc id="d">{escape(self.desc)}</desc>\n'
               f'<style>{STYLE}</style>\n<rect class="bg" width="{self.w}" height="{self.h}"/>\n'
               + "\n".join(self.p) + "\n</svg>\n")
        (HERE / name).write_text(svg)


def pico(d: Drawing, x: int, y: int, used: dict[int, str]) -> dict[int, tuple[int, int]]:
    """The Pico at (x, y), the pins of `used` (pin number: wire colour) in colour; returns
    each pin's centre."""
    h = PICO_H
    d.add(f'<rect x="{x}" y="{y}" width="{PICO_W}" height="{h}" rx="10" class="pico"/>')
    d.add(f'<rect x="{x + PICO_W / 2 - 24}" y="{y - 10}" width="48" height="26" rx="4" '
          f'class="usb"/>')
    d.text(x + PICO_W / 2, y + 36, "USB", "sub", "middle")
    d.text(x + PICO_W / 2, y + h - 34, "Raspberry Pi", "title", "middle")
    d.text(x + PICO_W / 2, y + h - 14, "Pico 2 W, from the top", "sub", "middle")
    at = {}
    for k, name in enumerate(PICO):
        n = k + 1
        left = n <= 20
        row = k if left else 39 - k
        cx = x + 16 if left else x + PICO_W - 16
        cy = y + 70 + row * ROW
        at[n] = (cx, cy)
        on = n in used
        if on:
            d.add(f'<circle cx="{cx}" cy="{cy}" r="7" fill="{used[n]}"/>')
        else:
            d.add(f'<circle cx="{cx}" cy="{cy}" r="6" class="free"/>')
        s = 1 if left else -1
        a = "start" if left else "end"
        d.text(cx + 14 * s, cy + 4, str(n), "numu" if on else "num", a)
        d.text(cx + 36 * s, cy + 4, name, "pinu" if on else "pin", a)
    return at


def bridge():
    """The five SPI wires and ground between the Pico and the Tang Nano."""
    wires = [  # Pico pin, signal, FPGA pin, header, colour, towards the Tang Nano
        (3, "GND", "GND", "J5 or J6", "#111827", None),
        (4, "SCLK", "73", "J6", "#d97706", True),
        (5, "MOSI", "74", "J6", "#2563eb", True),
        (6, "MISO", "75", "J5", "#16a34a", False),
        (7, "CS_N", "76", "J6", "#9333ea", True),
        (9, "HOST_IRQ", "86", "J5", "#0891b2", False),
    ]
    d = Drawing(900, 740, "Pico to Tang Nano 20K",
                "Six jumper wires: GP2 pin 4 to FPGA pin 73 (SCLK), GP3 pin 5 to 74 (MOSI), "
                "GP4 pin 6 to 75 (MISO), GP5 pin 7 to 76 (CS_N), GP6 pin 9 to 86 (HOST_IRQ), "
                "GND pin 3 to a GND pin of the Tang Nano.")
    at = pico(d, 600, 50, {w[0]: w[4] for w in wires})
    tx, tw = 60, 250
    d.add(f'<rect x="{tx}" y="50" width="{tw}" height="{PICO_H}" rx="10" '
          f'class="tang"/>')
    d.text(tx + 20, 84, "Tang Nano 20K", "title")
    d.text(tx + 20, 104, "FPGA pin number, header", "sub")
    for pin, sig, fpga, header, color, out in wires:
        _, cy = at[pin]
        d.line([(tx + tw, cy), (at[pin][0], cy)], color)
        d.dot(tx + tw, cy, color)
        d.text(tx + tw - 14, cy + 4, f"{fpga}  {header}", "term", "end")
        mid = (tx + tw + at[pin][0]) / 2
        d.text(mid, cy - 7, sig, "wire", "middle", color)
        if out is not None:
            d.arrow(mid + (52 if out else -52), cy, color, out)
    y = 50 + 70 + 10 * ROW
    for i, s in enumerate(["The pins of the chip, pio 0-7:",
                           "27 28 29 30 31  on J6",
                           "41 42 48  on J5",
                           "",
                           "Leave the LCD connector",
                           "empty: it shares them."]):
        d.text(tx + 20, y + i * 20, s, "note")
    d.text(450, 740 - 14, "Arrows: the way the signal goes. Both boards take power from their "
           "own USB cable.", "note", "middle")
    d.save("wiring_bridge.svg")


def diode(d: Drawing, x: int, y: int, color: str):
    """A diode on a wire going right, its stripe (cathode) on the right."""
    d.add(f'<rect x="{x - 26}" y="{y - 12}" width="52" height="24" rx="4" fill="#323f4b"/>')
    d.add(f'<rect x="{x + 14}" y="{y - 12}" width="7" height="24" fill="#d9dee4"/>')
    d.add(f'<polygon points="{x - 12},{y - 7} {x - 12},{y + 7} {x + 4},{y}" fill="#ffffff"/>')
    d.add(f'<rect x="{x + 4}" y="{y - 7}" width="2.5" height="14" fill="#ffffff"/>')


def resistor(d: Drawing, x: int, y: int):
    d.add(f'<rect x="{x - 26}" y="{y - 9}" width="52" height="18" rx="4" fill="#e9d8a6" '
          f'stroke="#8d6e2f" stroke-width="1.3"/>')
    for k in (-14, -6, 2):
        d.add(f'<rect x="{x + k}" y="{y - 9}" width="4" height="18" fill="#8d6e2f"/>')


def demo():
    """The IR receiver and the WS2812 ring: power from the Pico, the line to the Tang Nano."""
    red, black, amber, blue, green = "#dc2626", "#111827", "#f59e0b", "#2563eb", "#16a34a"
    d = Drawing(1000, 720, "The demo parts",
                "The Pico's VBUS (pin 40) through a 1N4007, stripe towards the ring, to the "
                "ring's 5V; the Pico's GND (pin 38) to the ring's GND and the IR receiver's "
                "minus; the Pico's 3V3(OUT) (pin 36) to the IR receiver's plus; the receiver's "
                "S to FPGA pin 27 (pio 0); FPGA pin 28 (pio 1) through 330 ohm to the ring's DI.")
    at = pico(d, 40, 50, {40: red, 38: black, 36: amber})
    px = at[40][0]
    vbus, gnd, v33 = at[40][1], at[38][1], at[36][1]

    # the ring, its 5V and GND straight across from VBUS and GND
    rx, ry, rw, rh = 470, vbus - 40, 210, 130
    d.add(f'<rect x="{rx}" y="{ry}" width="{rw}" height="{rh}" rx="10" class="part"/>')
    d.text(rx + rw / 2, ry + 26, "WS2812 ring", "title", "middle")
    d.text(rx + rw / 2, ry + 46, "12 LEDs", "sub", "middle")
    di = ry + rh - 30
    d.line([(px, vbus), (rx, vbus)], red)
    diode(d, 360, vbus, red)
    d.text(360, vbus - 20, "1N4007", "term", "middle")
    d.text(360, vbus + 30, "stripe towards the ring", "note", "middle")
    d.text(rx + 12, vbus + 4, "5V", "term")
    d.line([(px, gnd), (rx, gnd)], black)
    d.text(rx + 12, gnd + 4, "GND", "term")
    d.text(rx + rw - 12, di + 4, "DI", "term", "end")

    # the IR receiver below, its minus from the ground wire, its plus from 3V3
    ix, iy, iw, ih = 470, 330, 210, 140
    minus, plus, sig = iy + 40, iy + 90, iy + 65
    d.add(f'<rect x="{ix}" y="{iy}" width="{iw}" height="{ih}" rx="10" class="part"/>')
    d.text(ix + iw / 2, iy + ih - 34, "IR receiver", "title", "middle")
    d.text(ix + iw / 2, iy + ih - 14, "KY-022", "sub", "middle")
    bus = 420
    d.line([(bus, gnd), (bus, minus), (ix, minus)], black)
    d.dot(bus, gnd, black)
    d.text(ix + 12, minus + 4, "−", "term")
    d.line([(px, v33), (380, v33), (380, plus), (ix, plus)], amber)
    d.text(ix + 12, plus + 4, "+", "term")
    d.text(330, plus + 26, "3.3 V, never 5 V", "warn", "middle")
    d.text(ix + iw - 12, sig + 4, "S", "term", "end")

    # the Tang Nano on the right
    tx, tw = 800, 170
    d.add(f'<rect x="{tx}" y="{ry}" width="{tw}" height="{iy + ih - ry}" rx="10" class="tang"/>')
    d.text(tx + 16, ry + 26, "Tang Nano 20K", "title")
    d.text(tx + 16, ry + 46, "FPGA pin, chip pin", "sub")
    d.line([(rx + rw, di), (tx, di)], green)
    resistor(d, 740, di)
    d.text(740, di - 16, "330 Ω", "term", "middle")
    d.dot(tx, di, green)
    d.text(tx + 14, di + 4, "28  pio 1", "term")
    d.line([(ix + iw, sig), (tx, sig)], blue)
    d.dot(tx, sig, blue)
    d.text(tx + 14, sig + 4, "27  pio 0", "term")
    d.arrow(740, sig, blue, False)
    d.text(tx + 16, iy + ih - 34, "GND: the bridge's", "note")
    d.text(tx + 16, iy + ih - 16, "ground wire", "note")

    notes = ["The bridge stays wired as in wiring_bridge.svg: one ground for every board.",
             "Logic analyzer: D0 on pin 27 (the IR line), D1 on pin 28, its GND on any GND."]
    for i, s in enumerate(notes):
        d.text(470, 540 + i * 22, s, "note")
    d.save("wiring_demo.svg")


if __name__ == "__main__":
    bridge()
    demo()
