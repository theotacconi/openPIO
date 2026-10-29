.program iso7816_clk
; The card's clock (ISO/IEC 7816-3 CLK) on pin set_base: a square wave of 2 x CLKDIV clocks,
; 5 MHz from 50 MHz. Started with the reader (iso7816_reader.asm) in one CTRL write: the card's
; elementary time unit (372 of these clocks) is then 3720 clocks, the reader's 16 strobes.
.set_pins 1 1
.pin_mask 1
.clkdiv 5

    set pindirs, 1
.wrap_target
    set pins, 1
    set pins, 0
.wrap
