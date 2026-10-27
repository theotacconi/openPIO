.program uart_tx
; UART transmitter, 8N1, LSB first. Pin: set_base = out_base (TX).
; 8 strobes per bit: baud = clk / (8 x CLKDIV), CLKDIV fractional for exact rates.
; TX FIFO: one byte per word, bits 7:0. The line idles high; back-to-back bytes have exactly one
; stop bit when the FIFO is fed.
.set_pins 0 1            ; TX
.out_pins 0

    set pins, 1             ; idle high
    set pindirs, 1
.wrap_target
    pull block              ; stop bit / idle until a byte comes
    set pins, 0 [6]         ; start bit: 7 strobes + set x
    set x, 7
bit:
    out pins, 1 [6]         ; data bit: 7 strobes + jmp
    jmp x--, bit
    set pins, 1 [6]         ; stop bit: 7 strobes + pull
.wrap
