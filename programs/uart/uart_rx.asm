.program uart_rx
; UART receiver, 8N1, LSB first, with framing check. Pin: in_base (RX).
; 8 strobes per bit: baud = clk / (8 x CLKDIV). Cycles below count from the start edge.
; RX FIFO: one byte per word, bits 15:8 (ISR shifts right). A frame whose stop bit is low is
; dropped and raises IRQ 0.
.in_pins 3               ; RX

    wait 1 in_base 0        ; an idle line first: a low line at start is not a start bit
.wrap_target
    wait 0 in_base 0 [10]   ; start edge seen at cycle 1-2: sample bit 0 at cycle 12 (its middle)
    set x, 7
bit:
    in pins, 1 [6]          ; cycles 12, 20, ... 68
    jmp x--, bit            ; the last, not taken, ends at cycle 76: middle of the stop bit
    jmp in_base 0, good     ; stop bit high?
    irq 0                   ; framing error
    jmp idle
good:
    push noblock
idle:
    wait 1 in_base 0        ; back in wait 0 by cycle 79, before the next start bit
.wrap
