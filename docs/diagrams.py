"""Builds the documentation diagrams: docs/architecture.svg (the whole system) and one detailed
diagram per state-machine block in docs/blocks/. Written for developers new to digital logic:
every acronym is spelled out in the drawing or in its legend.

    python3 docs/diagrams.py
"""
from html import escape
from pathlib import Path

HERE = Path(__file__).resolve().parent

STYLE = """
.bg { fill: #ffffff; }
text { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; fill: #1f2933; }
.h { font-size: 15px; font-weight: 700; }
.d { font-size: 13px; fill: #3e4c59; }
.small { font-size: 11.5px; fill: #7b8794; }
.ld { font-size: 12.5px; fill: #1d5fbf; }
.lc { font-size: 12.5px; fill: #52606d; }
.term { font-size: 12.5px; font-weight: 700; fill: #1f2933; }
.def { font-size: 12.5px; fill: #3e4c59; }
.group { font-size: 12.5px; font-weight: 700; fill: #616e7c; }
.chiptitle { font-size: 17px; font-weight: 700; }
.smtitle { font-size: 15px; font-weight: 700; }
.b { fill: #ffffff; stroke: #9aa5b1; stroke-width: 1.5; }
.bar { fill: #f0f2f5; stroke: #9aa5b1; stroke-width: 1.5; }
.chip { fill: #f7f8fa; stroke: #323f4b; stroke-width: 2.5; }
.sm { fill: #ffffff; stroke: #616e7c; stroke-width: 1.8; }
.core { fill: #f7f8fa; stroke: #cbd2d9; stroke-width: 1.2; stroke-dasharray: 5 4; }
.q { fill: #e8f0fc; stroke: #1d5fbf; stroke-width: 1.5; }
.pau { fill: #eef7f0; stroke: #2f8a4c; stroke-width: 1.5; }
.sel { fill: #fff8e6; stroke: #b7791f; stroke-width: 1.5; }
.legend { fill: #f7f8fa; stroke: #cbd2d9; stroke-width: 1.2; }
.data { fill: none; stroke: #1d5fbf; stroke-width: 2.2; }
.ctl { fill: none; stroke: #7b8794; stroke-width: 1.6; }
.dash { fill: none; stroke: #1d5fbf; stroke-width: 1.8; stroke-dasharray: 6 5; }
.ah-data, .ah-dash { fill: #1d5fbf; }
.ah-ctl { fill: #7b8794; }
.cell { fill: #ffffff; stroke: #b7791f; stroke-width: 1.3; }
"""


class Drawing:
    def __init__(self, w, h, title, desc):
        self.w, self.h, self.title, self.desc, self.p = w, h, title, desc, []

    def add(self, s):
        self.p.append(s)

    def text(self, x, y, s, cls="d", anchor="start"):
        self.add(f'<text x="{x}" y="{y}" class="{cls}" text-anchor="{anchor}">{escape(s)}</text>')

    def box(self, x, y, w, h, title, lines=(), cls="b", gap=17, tcls="h"):
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" class="{cls}"/>')
        n = 1 + len(lines)
        y0 = y + h / 2 - (n - 1) * gap / 2 + 5
        if title:
            self.text(x + w / 2, y0, title, tcls, "middle")
        for i, ln in enumerate(lines):
            self.text(x + w / 2, y0 + (i + (1 if title else 0)) * gap, ln, "d", "middle")

    def sel(self, x, y, h, inputs, label=None, w=30, gap=None):
        """Selector (multiplexer): inputs on the left, one output in the middle of the right side.
        Returns the output point."""
        self.add(f'<polygon points="{x},{y} {x + w},{y + 14} {x + w},{y + h - 14} {x},{y + h}" class="sel"/>')
        if label:
            self.text(x + w / 2, y - 8, label, "small", "middle")
        gap = gap or (h - 24) / max(1, len(inputs) - 1) if len(inputs) > 1 else 0
        for i, s in enumerate(inputs):
            yy = y + 12 + i * gap if len(inputs) > 1 else y + h / 2
            if s:
                self.text(x - 10, yy + 4, s, "d", "end")
            self.arrow([(x - 8, yy), (x - 1, yy)], "ctl", head=False)
        return x + w, y + h / 2

    def arrow(self, pts, kind="data", label=None, at=None, anchor="middle", both=False, head=True):
        d = " ".join(f"{x},{y}" for x, y in pts)
        m = (f' marker-end="url(#{kind})"' if head else "") + (f' marker-start="url(#{kind}s)"' if both else "")
        self.add(f'<polyline points="{d}" class="{kind}"{m}/>')
        if label:
            self.text(at[0], at[1], label, "ld" if kind in ("data", "dash") else "lc", anchor)

    def table(self, x, y, rows, cols, gap=24):
        """Rows of cells, left-aligned at the column x positions; the first cell in bold."""
        for i, row in enumerate(rows):
            for k, cell in enumerate(row):
                self.text(cols[k], y + i * gap, cell, "term" if k == 1 else "def")

    def legend(self, x, y, w, symbols, terms, cols=2):
        """Symbols on the first row, then the terms in columns."""
        rows = (len(terms) + cols - 1) // cols
        h = 58 + rows * 21
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" class="legend"/>')
        self.text(x + 16, y + 24, "Legend", "group")
        sx = x + 90
        for kind, s in symbols:
            yy = y + 20
            if kind == "sel":
                self.add(f'<polygon points="{sx},{yy - 10} {sx + 16},{yy - 4} {sx + 16},{yy + 4} {sx},{yy + 10}" class="sel"/>')
            else:
                self.arrow([(sx, yy), (sx + 34, yy)], kind)
            self.text(sx + (24 if kind == "sel" else 44), yy + 4, s, "def")
            sx += (24 if kind == "sel" else 44) + 7.2 * len(s) + 30
        colw = (w - 32) / cols
        for i, (t, dfn) in enumerate(terms):
            cx, cy = x + 16 + (i // rows) * colw, y + 54 + (i % rows) * 21
            self.add(f'<text x="{cx}" y="{cy}" class="def"><tspan class="term">{escape(t)}</tspan>'
                     f'  {escape(dfn)}</text>')
        return h

    def svg(self):
        defs = "".join(
            f'<marker id="{k}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="{s}" markerHeight="{s}" orient="auto">'
            f'<path d="M0,0 L10,5 L0,10 z" class="ah-{k}"/></marker>'
            f'<marker id="{k}s" viewBox="0 0 10 10" refX="1" refY="5" markerWidth="{s}" markerHeight="{s}" orient="auto">'
            f'<path d="M10,0 L0,5 L10,10 z" class="ah-{k}"/></marker>'
            for k, s in (("data", 6), ("ctl", 7), ("dash", 6)))
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" width="{self.w}" '
                f'height="{self.h}" role="img" aria-labelledby="t d">\n<title id="t">{escape(self.title)}</title>\n'
                f'<desc id="d">{escape(self.desc)}</desc>\n<style>{STYLE}</style>\n<defs>{defs}</defs>\n'
                f'<rect x="0" y="0" width="{self.w}" height="{self.h}" rx="18" class="bg"/>\n'
                + "\n".join(self.p) + "\n</svg>\n")


DATA_CTL = [("data", "data"), ("ctl", "control, settings")]
SELECTOR = ("sel", "selector: passes one input, chosen by the instruction")


# ---------------------------------------------------------------------- the whole system

def architecture():
    g = Drawing(1480, 920, "openPIO architecture",
                "A host microcontroller talks over SPI to the openPIO chip. Inside, a host interface "
                "feeds two identical state machines; each has a TX FIFO and OSR that shift data out to "
                "the pins, an ISR and RX FIFO that collect data in, a core that runs a 64-instruction "
                "program, and an optional PAU on the line. A pin-control block shares 16 pin positions "
                "(14 on the chip) with the circuit.")
    g.add('<g transform="translate(20,-40)">')
    # legend, centred
    g.arrow([(560, 90), (610, 90)], "data"); g.text(620, 95, "data")
    g.arrow([(720, 90), (770, 90)], "ctl"); g.text(780, 95, "control, settings")
    # host
    g.box(20, 330, 200, 250, "Your microcontroller", ["(the host)", "", "loads the programs", "sets the options",
                                                      "sends and receives data", "is told when to act"])
    g.arrow([(220, 425), (318, 425)], "ctl", both=True)
    g.text(262, 412, "SPI", "lc", "middle"); g.text(262, 446, "4 wires", "small", "middle")
    g.arrow([(318, 500), (222, 500)], "ctl")
    g.text(262, 487, "interrupt", "lc", "middle"); g.text(262, 521, "HOST_IRQ", "small", "middle")
    # chip
    g.add('<rect x="300" y="120" width="880" height="800" rx="14" class="chip"/>')
    g.text(322, 150, "openPIO chip", "chiptitle")
    g.box(320, 170, 130, 730, "Host interface", ["SPI + registers", "", "write program", "set options",
                                                 "start / stop / step", "move FIFO data", "read status"],
          cls="bar", gap=19)
    g.box(1030, 170, 130, 730, "Pin control", ["16 pins, shared", "", "direction", "(in or out)", "open drain",
                                               "input sync", "(2 clocks)"], cls="bar", gap=19)
    g.add('<rect x="475" y="182" width="530" height="560" rx="12" class="sm"/>')
    g.text(495, 210, "State machine 0", "smtitle")
    g.text(985, 210, "runs one program, one instruction per step", "small", "end")
    g.box(495, 232, 130, 76, "TX FIFO", ["data to send", "133 words"], cls="q", gap=18)
    g.box(665, 232, 130, 76, "OSR", ["shifts the bits", "out, one by one"], cls="q", gap=18)
    g.arrow([(452, 270), (493, 270)]); g.arrow([(625, 270), (663, 270)])
    g.arrow([(795, 270), (1028, 270)], "data", "OUT: bits to the pins", (910, 260))
    g.box(495, 634, 130, 76, "RX FIFO", ["data received", "133 words"], cls="q", gap=18)
    g.box(665, 634, 130, 76, "ISR", ["collects the bits", "in, one by one"], cls="q", gap=18)
    g.arrow([(1028, 682), (797, 682)], "data", "IN: bits from the pins", (912, 702))
    g.arrow([(663, 672), (627, 672)]); g.arrow([(493, 672), (452, 672)])
    g.box(835, 360, 150, 206, "PAU", ["optional helper", "on the line:", "encoding,", "bit stuffing,", "CRC check"],
          cls="pau", gap=19)
    g.arrow([(870, 270), (870, 358)])
    g.text(878, 320, "or through", "ld"); g.text(878, 336, "the PAU", "ld")
    g.arrow([(985, 420), (1028, 420)]); g.arrow([(1028, 505), (987, 505)])
    g.arrow([(870, 566), (870, 656), (797, 656)])
    g.add('<rect x="495" y="330" width="310" height="280" rx="10" class="core"/>')
    g.text(510, 352, "Core: executes your program", "group")
    g.box(510, 366, 135, 66, "Program", ["64 instructions"], gap=19)
    g.box(660, 366, 130, 66, "Control", ["decodes, steps"], gap=19)
    g.box(510, 446, 135, 66, "Clock divider", ["sets the step rate"], gap=19)
    g.box(660, 446, 130, 66, "X, Y + ALU", ["counters, math"], gap=19)
    g.box(510, 526, 135, 66, "Conditions", ["4 tests: pins, flags"], gap=19)
    g.box(660, 526, 130, 66, "IRQ flags", ["8 signals"], gap=19)
    g.arrow([(645, 399), (658, 399)], "ctl"); g.arrow([(452, 399), (508, 399)], "ctl")
    g.arrow([(730, 330), (730, 310)], "ctl"); g.text(738, 324, "out", "lc")
    g.arrow([(730, 610), (730, 632)], "ctl"); g.text(738, 626, "in", "lc")
    g.add('<rect x="475" y="790" width="530" height="96" rx="12" class="sm"/>')
    g.text(495, 818, "State machine 1", "smtitle")
    g.text(495, 842, "Identical to state machine 0. The two run side by side, on the same pins,")
    g.text(495, 862, "and can start on the same clock (4 state machines in a larger build).")
    g.arrow([(740, 744), (740, 788)], "ctl", both=True)
    g.text(752, 763, "peer link: IRQ flags", "ld")
    g.arrow([(1005, 838), (1028, 838)]); g.arrow([(452, 838), (473, 838)], "ctl")
    g.arrow([(1160, 470), (1258, 470)], "data", both=True)
    g.text(1209, 457, "14 pins", "ld", "middle"); g.text(1209, 491, "uio, uo, ui", "small", "middle")
    g.box(1260, 350, 160, 240, "Your circuit", ["", "LEDs, sensors,", "displays, radios,", "buses: UART, SPI,",
                                                "I2C, USB, CAN, …"])
    g.add("</g>")
    return g


# ---------------------------------------------------------------------- program and control

def core():
    g = Drawing(1240, 700, "Program and control",
                "The clock divider makes the steps; the program counter picks the instruction; the "
                "program memory gives it; its three fields go to the control, to the delay and side-set "
                "logic, and to the blocks as arguments.")
    g.box(40, 40, 230, 80, "Clock divider", ["CLKDIV: 1 to 255.99 clocks", "→ one step"])
    g.arrow([(270, 80), (1000, 80), (1000, 128)], "ctl", "step: every block below acts only on a step",
            (640, 70))
    ox, oy = g.sel(330, 170, 200, ["PC + 1, or back to the loop start", "JMP: the jump target",
                                   "OUT PC: a computed jump", "host write, or RESET → 0"],
                   label="next instruction")
    g.arrow([(ox, oy), (418, oy)])
    g.box(420, 235, 130, 70, "PC", ["Program Counter", "instruction 0 … 63"])
    g.arrow([(550, 270), (598, 270)], "data", "address", (574, 262))
    g.box(600, 215, 190, 110, "Program memory", ["64 instructions", "of 16 bits each", "written by the host"])
    g.arrow([(790, 270), (858, 270)], "data", "instruction", (824, 262))
    g.add('<line x1="860" y1="150" x2="860" y2="420" class="data"/>')
    g.text(868, 168, "bits 15–13", "ld")
    g.arrow([(860, 175), (938, 175)])
    g.box(940, 130, 260, 90, "Control", ["decides what every block does", "on this step, or waits (stall)"])
    g.text(868, 268, "bits 12–8", "ld")
    g.arrow([(860, 275), (938, 275)])
    g.box(940, 240, 260, 80, "Delay and side-set", ["after it: wait n × DLYMUL steps", "with it: set some pins"])
    g.text(868, 368, "bits 7–0", "ld")
    g.arrow([(860, 375), (938, 375)])
    g.text(946, 371, "which register, how many bits,"); g.text(946, 389, "which condition, the jump target …")
    g.box(940, 420, 260, 120, "It waits (stalls) while", ["a WAIT condition is false", "the FIFO it needs is empty / full",
                                                          "the PAU queue lacks room / bits", "an IRQ it waits on is set"],
          gap=19)
    g.arrow([(1200, 175), (1222, 175), (1222, 480), (1202, 480)], "ctl")
    g.legend(40, 570, 1160, DATA_CTL + [SELECTOR],
             [("PC", "Program Counter: number of the instruction being run"),
              ("CLKDIV", "clock divider: clocks per step, set by the host"),
              ("JMP", "jump instruction; OUT PC jumps to bits taken from the OSR"),
              ("DLYMUL", "delay multiplier, set by the program (SET)"),
              ("RESET", "restart command from the host (register CTRL)"),
              ("side-set", "pins changed by the instruction itself, at no extra time")])
    return g


# ---------------------------------------------------------------------- X, Y and ALU

def alu():
    g = Drawing(1240, 900, "X, Y and the ALU",
                "X and Y are two 16-bit registers with a selector for what they load; comparisons feed the "
                "conditions. The ALU combines the destination's current value with a source and writes the "
                "result back to the destination.")
    g.text(40, 36, "X and Y: two 16-bit working registers (counters, constants, temporary values)", "group")
    ox, oy = g.sel(260, 60, 170, ["SET: a constant 0 … 31", "ALU result", "bits taken by OUT",
                                  "X − 1 (JMP X--: count down)", "host (machine stopped)"], label="what X loads")
    g.arrow([(ox, oy), (358, oy)])
    g.box(360, 110, 90, 70, "X", ["16 bits"])
    g.text(405, 206, "Y: same selector", "small", "middle")
    g.arrow([(450, 145), (538, 145)])
    g.box(540, 70, 230, 150, "Comparisons", ["X == 0,  Y == 0", "X == Y", "bit n of X or of Y"], gap=22)
    g.arrow([(770, 145), (898, 145)], "ctl", "to the conditions", (834, 135))
    g.box(900, 110, 300, 70, "", ["used by JMP and WAIT, e.g.", "jmp x--, loop   ·   wait 1 x 3"])

    g.text(40, 270, "ALU: Arithmetic Logic Unit, one operation per instruction:  destination = D  op  S", "group")
    ox, oy = g.sel(260, 300, 200, ["pins (outputs)", "pin directions", "X", "Y", "ISR", "OSR", "NULL → 0", "COND → 0"],
                   label="D: current value of the destination")
    g.arrow([(ox, oy), (330, oy), (330, 460), (398, 460)])
    ox2, oy2 = g.sel(260, 540, 200, ["pins (inputs)", "pin directions", "X", "Y", "ISR", "OSR", "NULL → 0", "STATUS"],
                     label="S: the source")
    g.arrow([(ox2, oy2), (330, oy2), (330, 580), (398, 580)])
    g.add('<polygon points="400,420 500,470 500,570 400,620 400,540 422,520 400,500" class="sel"/>')
    g.text(458, 525, "ALU", "h", "middle")
    g.arrow([(450, 690), (450, 600)], "ctl")
    g.text(450, 708, "operation (from the instruction)", "small", "middle")
    g.arrow([(500, 520), (578, 520)], "data", "result", (539, 510))
    g.add('<line x1="580" y1="400" x2="580" y2="660" class="data"/>')
    outs = ["X or Y", "ISR (then counted as full)", "OSR (then counted as full)",
            "pins or directions (all 8, within PIN_MASK)", "COND: rewrites one condition", "NULL: thrown away"]
    for i, s in enumerate(outs):
        y = 410 + i * 48
        g.arrow([(580, y), (608, y)]); g.text(614, y + 4, s)
    g.box(900, 300, 300, 330, "", [])
    g.text(1050, 332, "Operations", "h", "middle")
    g.table(0, 368, [("", "MOV", "copy S"), ("", "REV", "S, bit order reversed"), ("", "AND", "D and S, bit by bit"),
                     ("", "OR", "D or S"), ("", "XOR", "D xor S"), ("", "XNOR", "not (D xor S)"),
                     ("", "ADD", "D + S"), ("", "SUB", "D − S")], [0, 924, 990], gap=26)
    g.text(924, 590, "NOT d is written as XNOR d, NULL", "small")
    g.legend(40, 775, 1160, DATA_CTL + [SELECTOR],
             [("ISR / OSR", "Input / Output Shift Register"), ("NULL", "nothing: reads 0, a write is lost"),
              ("COND", "the 4 programmable conditions"), ("STATUS", "16 status bits (conditions page)"),
              ("PIN_MASK", "the pins this state machine may change"), ("16 bits", "all values are 16-bit numbers")],
             cols=3)
    return g


# ---------------------------------------------------------------------- conditions

def conditions():
    g = Drawing(1240, 780, "Conditions",
                "Each of the 4 condition slots picks a signal (a pin, a flag, a bit of X or Y, a status bit), "
                "may invert it, and gives a true/false value to JMP and WAIT; WAIT can also wait for a change.")
    g.text(40, 36, "4 slots (COND0 … COND3), each one test. The host sets them; a program can rewrite one.", "group")
    ox, oy = g.sel(300, 70, 240, ["pin n", "pin in_base + n", "IRQ flag n (own)", "IRQ flag n (other machine)",
                                  "bit n of X", "bit n of Y", "SM status bit n", "PAU status bit n"],
                   label="what to test")
    g.arrow([(ox, oy), (398, oy)])
    g.box(400, 155, 120, 70, "Invert?", ["true ↔ false"])
    g.arrow([(520, 190), (598, 190)])
    g.box(600, 155, 150, 70, "Value now", ["true or false"])
    g.arrow([(750, 175), (848, 175)]); g.text(856, 179, "JMP: jump if true")
    g.box(600, 265, 150, 70, "Value at the", ["previous step"])
    g.arrow([(675, 225), (675, 263)], "ctl")
    g.arrow([(750, 300), (800, 300), (800, 210), (848, 210)]); g.text(856, 205, "WAIT: until true or false,")
    g.text(856, 222, "or until it changes (an edge)")
    g.box(860, 270, 340, 80, "Timeout", ["a WAIT … timeout also ends after", "TIMEOUT_VAL steps; sets the timeout flag"])
    g.box(40, 360, 520, 280, "", [])
    g.text(300, 390, "SM status bits (SM = state machine)", "h", "middle")
    g.table(0, 422, [("0", "x_zero", "X == 0"), ("1", "y_zero", "Y == 0"), ("2", "x_eq_y", "X == Y"),
                     ("3", "timeout", "a WAIT timed out"), ("4", "osr_empty", "OSR used up"),
                     ("5", "isr_full", "ISR filled"), ("6", "tx_empty", "TX FIFO empty"),
                     ("7", "rx_full", "RX FIFO full")], [70, 100, 220], gap=26)
    g.box(600, 360, 600, 280, "", [])
    g.text(900, 390, "PAU status bits", "h", "middle")
    g.table(0, 422, [("0", "tx_idle", "everything sent"), ("1", "rx_idle", "line quiet"),
                     ("2", "tx_ready", "room in the TX queue"), ("3", "rx_ready", "bits received"),
                     ("4", "crc_ok", "CRC correct"), ("5", "crc_zero", "CRC is 0")], [630, 655, 740], gap=26)
    g.table(0, 422, [("6", "stuff_err", "bad stuffing bit"), ("7", "overrun", "bits were lost"),
                     ("8", "se0", "both lines low"), ("9", "se1", "both lines high"),
                     ("10", "bit_err", "line ≠ bit sent")], [905, 935, 1020], gap=26)
    g.legend(40, 670, 1160, DATA_CTL + [SELECTOR],
             [("JMP / WAIT", "jump if true / pause until true"), ("IRQ", "Interrupt ReQuest flag"),
              ("SM", "state machine"), ("PAU", "Protocol Acceleration Unit"),
              ("in_base", "first input pin of this machine"), ("CRC", "Cyclic Redundancy Check (a checksum)")],
             cols=3)
    return g


# ---------------------------------------------------------------------- IRQ flags

def irq():
    g = Drawing(1240, 560, "IRQ flags",
                "Eight flags per state machine, set and cleared by the program, by the other state "
                "machine and by the host; they are tested by conditions and, through IRQ_MASK, raise "
                "the HOST_IRQ pin, as do the FIFO levels.")
    ins = [("irq n / irq clear n", "this state machine"), ("irq peer n", "the other state machine"),
           ("IRQ register", "the host sets or clears"), ("RESET", "clears all")]
    for i, (a, b) in enumerate(ins):
        y = 70 + i * 60
        g.text(40, y, a, "term"); g.text(40, y + 18, b, "small")
        g.arrow([(240, y + 4), (338, y + 4)], "ctl")
    g.box(340, 50, 300, 230, "", [])
    g.text(490, 80, "IRQ flags: 8 signals, 0 … 7", "h", "middle")
    for k in range(8):
        x = 362 + k * 33
        g.add(f'<rect x="{x}" y="110" width="27" height="40" rx="4" class="cell"/>')
        g.text(x + 13.5, 135, str(k), "d", "middle")
    g.text(490, 186, "each one: set or clear", "d", "middle")
    g.text(490, 206, "the other machine sees them too", "small", "middle")
    g.arrow([(640, 100), (738, 100)], "ctl"); g.text(746, 96, "conditions: irq n, peer_irq n"); g.text(746, 114, "(JMP, WAIT)", "small")
    g.arrow([(640, 160), (738, 160)], "ctl"); g.text(746, 156, "irq wait n: the program pauses"); g.text(746, 174, "until someone clears flag n", "small")
    g.arrow([(640, 240), (680, 240), (680, 330), (738, 330)], "ctl")
    g.box(740, 300, 160, 60, "IRQ_MASK", ["chooses what counts"])
    g.text(560, 382, "RX FIFO not empty  (mask bit 8)", "d", "end"); g.arrow([(570, 378), (700, 378), (700, 350), (738, 350)], "ctl")
    g.text(560, 412, "TX FIFO ≤ 64 words  (mask bit 9)", "d", "end"); g.arrow([(570, 408), (720, 408), (720, 356), (738, 356)], "ctl")
    g.arrow([(900, 330), (978, 330)], "ctl")
    g.box(980, 300, 210, 60, "Any of them", ["(and the other machine's)"])
    g.arrow([(1085, 360), (1085, 418)], "ctl")
    g.text(1085, 438, "HOST_IRQ pin → the host", "term", "middle")
    g.legend(40, 470, 1160, [("ctl", "control signals")],
             [("IRQ", "Interrupt ReQuest: a flag that asks for attention"),
              ("IRQ_MASK", "host register: which requests reach HOST_IRQ"),
              ("HOST_IRQ", "the chip's interrupt pin to the host"),
              ("FIFO", "First In, First Out queue of data words")])
    return g


# ---------------------------------------------------------------------- OSR (output)

def osr():
    g = Drawing(1240, 600, "OSR: the output path",
                "The OSR loads a word from the TX FIFO (or another source); each OUT shifts n bits out of "
                "it to a destination: the pins, the PAU, a register or the program counter. A counter "
                "tells when the OSR is used up so it can reload by itself.")
    g.box(40, 30, 180, 56, "TX FIFO", ["data from the host"], cls="q")
    ox, oy = g.sel(330, 40, 210, ["", "rest of the OSR (after an OUT)", "X (PULL noblock, FIFO empty)",
                                  "ALU result (mov osr, …)", "the other machine's RX FIFO"],
                   label="what the OSR loads")
    g.arrow([(220, 58), (321, 58)], "data", "PULL: next word", (270, 50))
    g.arrow([(ox, oy), (398, oy)])
    g.box(400, 110, 150, 80, "OSR", ["Output Shift Register", "16 bits"], cls="q")
    g.arrow([(550, 150), (618, 150)])
    g.box(620, 100, 180, 100, "Shifter", ["takes n bits (1 … 16)", "right: lowest bit first", "left: highest bit first"])
    g.arrow([(800, 150), (868, 150)], "data", "n bits", (834, 140))
    g.add('<line x1="870" y1="50" x2="870" y2="250" class="data"/>')
    outs = ["pins, from out_base", "PAU transmitter (when TX is on)", "pin directions", "X or Y",
            "ISR", "PC: jump there", "NULL: thrown away"]
    for i, s in enumerate(outs):
        y = 56 + i * 32
        g.arrow([(870, y), (898, y)]); g.text(904, y + 4, s)
    g.text(870, 36, "where the bits go (OUT dest, n)", "small")
    g.box(400, 300, 170, 70, "Bits used", ["counter, 0 … 16"])
    g.text(400, 394, "+n on each OUT · 0 when the OSR loads", "small")
    g.arrow([(570, 335), (638, 335)], "ctl")
    g.box(640, 300, 200, 70, "≥ pull threshold?", ["threshold: 1 … 16 (host)"])
    g.arrow([(840, 335), (898, 335)], "ctl"); g.text(906, 331, "OSR empty: OUT … auto loads the next")
    g.text(906, 349, "word first (autopull); also a condition", "small")
    g.legend(40, 440, 1160, DATA_CTL + [SELECTOR],
             [("OSR", "Output Shift Register"), ("FIFO", "First In, First Out queue"),
              ("PULL", "load the OSR from the TX FIFO"), ("OUT", "send n bits from the OSR"),
              ("out_base", "first output pin of this machine"), ("PAU / TX", "Protocol Acceleration Unit / transmit"),
              ("PC", "Program Counter"), ("noblock", "do not wait: use X when the FIFO is empty")])
    return g


# ---------------------------------------------------------------------- ISR (input)

def isr():
    g = Drawing(1240, 600, "ISR: the input path",
                "Each IN shifts n bits from a source (the pins, the PAU receiver, a register) into the ISR; "
                "a PUSH moves the ISR into the RX FIFO. A counter tells when the ISR is full so it can "
                "push by itself.")
    ox, oy = g.sel(330, 40, 240, ["pins, from in_base", "PAU receiver (when RX is on)", "X", "Y", "ISR", "OSR",
                                  "STATUS (16 status bits)", "NULL: zeros"], label="IN source, n")
    g.arrow([(ox, oy), (398, oy)])
    g.box(400, 120, 180, 80, "Shifter", ["adds n bits (1 … 16)", "right / left as set"])
    g.arrow([(580, 160), (648, 160)])
    g.box(650, 120, 150, 80, "ISR", ["Input Shift Register", "16 bits"], cls="q")
    g.text(650, 230, "also: mov isr, … (ALU) · out isr, n", "small")
    g.text(650, 246, "a PUSH clears it", "small")
    g.add('<line x1="800" y1="160" x2="860" y2="160" class="data"/><line x1="860" y1="60" x2="860" y2="260" class="data"/>')
    outs = [("RX FIFO: PUSH, to the host", True), ("ALU, OSR (as a source)", False),
            ("PAU CRC (crc_feed n)", False)]
    for i, (s, _) in enumerate(outs):
        y = 70 + i * 60
        g.arrow([(860, y), (888, y)]); g.text(894, y + 4, s)
    g.box(400, 300, 170, 70, "Bits collected", ["counter, 0 … 16"])
    g.text(400, 394, "+n on each IN · 0 after a PUSH", "small")
    g.arrow([(570, 335), (638, 335)], "ctl")
    g.box(640, 300, 200, 70, "≥ push threshold?", ["threshold: 1 … 16 (host)"])
    g.arrow([(840, 335), (898, 335)], "ctl"); g.text(906, 331, "ISR full: IN … auto pushes it to the")
    g.text(906, 349, "RX FIFO by itself (autopush); also a condition", "small")
    g.legend(40, 440, 1160, DATA_CTL + [SELECTOR],
             [("ISR", "Input Shift Register"), ("FIFO", "First In, First Out queue"),
              ("IN", "take n bits into the ISR"), ("PUSH", "move the ISR into the RX FIFO"),
              ("in_base", "first input pin of this machine"), ("PAU / RX", "Protocol Acceleration Unit / receive"),
              ("CRC", "Cyclic Redundancy Check (a checksum)"), ("STATUS", "status bits, see the conditions page")])
    return g


# ---------------------------------------------------------------------- FIFOs

def fifo():
    g = Drawing(1240, 640, "TX and RX FIFOs",
                "Each state machine has two queues of 133 words. Next to the machine, 4 fast words; behind "
                "them, a 128-word ring in a memory chip (SRAM) shared by the two queues, which moves one "
                "word per clock; an IN or OUT register meets the host. When the memory part is empty, words "
                "skip it.")
    g.text(40, 36, "TX FIFO: words to send  (133 = 1 IN register + 128 in memory + 4 front)", "group")
    g.text(40, 104, "host (TX_PUSH)", "term"); g.text(40, 122, "or the other machine", "small")
    g.arrow([(190, 110), (258, 110)])
    g.box(260, 85, 100, 50, "IN register", [])
    g.box(470, 60, 260, 300, "", [])
    g.text(600, 90, "Data memory (SRAM)", "h", "middle")
    g.text(600, 110, "256 words, one access per clock", "small", "middle")
    g.box(490, 125, 220, 60, "TX ring", ["128 words"], cls="q")
    g.box(490, 235, 220, 60, "RX ring", ["128 words"], cls="q")
    g.text(600, 330, "in turn: TX in · TX out · RX in · RX out", "small", "middle")
    g.arrow([(360, 110), (488, 155)])
    g.arrow([(710, 155), (838, 110)])
    g.box(840, 85, 120, 50, "TX front", ["4 words"], cls="q")
    g.arrow([(960, 110), (1028, 110)]); g.text(1036, 105, "PULL, OUT … auto", "term"); g.text(1036, 123, "the machine", "small")
    g.arrow([(310, 85), (310, 52), (900, 52), (900, 83)], "dash", "memory part empty: skip it", (600, 46))
    g.text(40, 400, "RX FIFO: words received  (133 = 4 front + 128 in memory + 1 OUT register)", "group")
    g.text(1036, 300, "PUSH, IN … auto", "term"); g.text(1036, 318, "the machine", "small")
    g.arrow([(1028, 305), (962, 305)])
    g.box(840, 280, 120, 50, "RX front", ["4 words"], cls="q")
    g.arrow([(838, 305), (712, 265)])
    g.arrow([(488, 265), (362, 305)])
    g.box(260, 280, 100, 50, "OUT register", [])
    g.arrow([(258, 305), (192, 305)]); g.text(40, 300, "host (RX_POP)", "term"); g.text(40, 318, "or the other machine", "small")
    g.arrow([(900, 332), (900, 372), (310, 372), (310, 333)], "dash", "memory part empty: skip it", (600, 390))
    g.box(40, 430, 1160, 70, "", ["Why it matters: the host and the other machine never touch the memory directly, so the "
                                  "machine never waits for them.", "A program moving more than about one word every "
                                  "2 clocks can briefly wait for the memory."], gap=20)
    g.legend(40, 520, 1160, [("data", "data"), ("dash", "shortcut when the memory part is empty")],
             [("FIFO", "First In, First Out: a queue, words leave in the order they came"),
              ("SRAM", "Static RAM: the memory chip holding the long part of the queues"),
              ("TX / RX", "transmit (host → pins) / receive (pins → host)"),
              ("PUSH / PULL", "the program puts a word in / takes a word out")])
    return g


# ---------------------------------------------------------------------- PAU

def pau():
    g = Drawing(1240, 760, "PAU: Protocol Acceleration Unit",
                "An optional serial engine between the shift registers and the pins. Transmitting, it "
                "queues the bits, inserts stuffing bits and encodes the line; receiving, it recovers the "
                "bit timing, removes the stuffing bits and queues the bits. A CRC unit checks or appends a "
                "checksum; a monitor stops sending if the line disagrees.")
    g.text(40, 36, "Transmit (when TX is on)", "group")
    g.text(40, 84, "OUT pins, n", "term"); g.text(40, 102, "bits from the OSR", "small")
    g.arrow([(170, 90), (218, 90)])
    g.box(220, 60, 170, 60, "TX queue", ["32 bits waiting"], cls="q")
    g.arrow([(390, 90), (438, 90)])
    g.box(440, 55, 220, 70, "Stuffing", ["adds a bit after a run", "of N bits (USB, CAN)"], cls="pau")
    g.arrow([(660, 90), (708, 90)])
    g.box(710, 55, 230, 70, "Encoder", ["bit → line: NRZ, NRZI,", "Manchester, BMC"], cls="pau")
    g.arrow([(940, 90), (1008, 90)]); g.text(1016, 86, "pin out_base", "term"); g.text(1016, 104, "(+1 inverted, DIFF)", "small")
    g.text(40, 196, "Receive (when RX is on)", "group")
    g.text(1016, 244, "pin in_base", "term"); g.text(1016, 262, "(+1 for DIFF)", "small")
    g.arrow([(1008, 250), (942, 250)])
    g.box(710, 215, 230, 70, "Decoder", ["finds the bit timing", "from the line's edges"], cls="pau")
    g.arrow([(708, 250), (662, 250)])
    g.box(440, 215, 220, 70, "Destuffing", ["removes the added bit;", "a wrong one: stuff_err"], cls="pau")
    g.arrow([(438, 250), (392, 250)])
    g.box(220, 220, 170, 60, "RX queue", ["32 bits waiting"], cls="q")
    g.arrow([(218, 250), (172, 250)]); g.text(40, 244, "IN pins, n", "term"); g.text(40, 262, "bits to the ISR", "small")
    g.box(220, 330, 440, 80, "CRC: checksum, 1 to 32 bits", ["fed by the data bits sent or received, or by the ISR",
                                                              "appends it when sending · checks it when receiving"])
    g.arrow([(415, 90), (415, 328)], "ctl"); g.arrow([(560, 287), (560, 328)], "ctl")
    g.box(710, 330, 230, 80, "Monitor", ["compares each bit sent with", "pin in_base: bit_err, stop"])
    g.box(40, 450, 560, 110, "The program controls it", ["SET pau_ctl: tx, rx, stuff, crc, monitor (on / off)",
                                                          "SET pau_baud: H = steps per half bit",
                                                          "pau crc_reset · crc_append · tx_abort · crc_feed n"], gap=22)
    g.box(640, 450, 560, 110, "The host sets it up", ["PAU_MODE: line code, invert, stuffing rule, baud",
                                                     "CRC_CFG, CRC_POLY: which checksum",
                                                     "CRC_RES: the value of a correct frame"], gap=22)
    g.text(1200, 436, "11 status bits go to the conditions", "small", "end")
    g.legend(40, 590, 1160, DATA_CTL,
             [("PAU", "Protocol Acceleration Unit"), ("TX / RX", "transmit / receive"),
              ("NRZ", "Non-Return-to-Zero: the line is the bit"), ("NRZI", "NRZ Inverted: a 0 flips the line (USB)"),
              ("Manchester", "two halves per bit, 1 = low→high (Ethernet)"),
              ("BMC", "Biphase Mark Code: a flip every bit, two for a 1"),
              ("DIFF", "differential: a second pin with the opposite level"),
              ("CRC", "Cyclic Redundancy Check: a checksum")])
    return g


if __name__ == "__main__":
    (HERE / "blocks").mkdir(exist_ok=True)
    (HERE / "architecture.svg").write_text(architecture().svg())
    for name, fn in [("core", core), ("alu", alu), ("conditions", conditions), ("irq", irq), ("osr", osr),
                     ("isr", isr), ("fifo", fifo), ("pau", pau)]:
        (HERE / "blocks" / f"{name}.svg").write_text(fn().svg())
    print("ok")
