.program uart_rx
; UART receiver, 8N1, LSB first, with framing check. Pin: in_base (RX).
; 8 strobes per bit: baud = clk / (8 x CLKDIV). Cycles below count from the start edge.
; RX FIFO: one byte per word, bits 15:8 (ISR shifts right). A frame whose stop bit is low is
; dropped and raises IRQ 0. Back to back, a transmitter up to 3 % fast or slow is read: the
; stop bit is sampled at cycle 75 (in it from 74.2 when 3 % slow) and the program is back in
; wait 0 by cycle 77, before the next start bit (cycle 80; 77.6 when 3 % fast), so a late
; start does not carry over to the next frame. It waits for an idle line only after a low
; stop bit.
.in_pins 3               ; RX

    wait 1 in_base 0        ; an idle line first: a low line at start is not a start bit
.wrap_target
start:
    wait 0 in_base 0 [10]   ; start edge seen at cycle 1-2: sample bit 0 at cycle 12 (its middle)
    set x, 6
bit:
    in pins, 1 [6]          ; cycles 12, 20, ... 60: bits 0..6
    jmp x--, bit
    in pins, 1 [6]          ; cycle 68: bit 7
    jmp in_base 0, good     ; cycle 75: the stop bit high?
    irq 0                   ; framing error
    wait 1 in_base 0        ; the line low: wait until it is idle again
    jmp start
good:
    push noblock            ; cycle 76
.wrap
