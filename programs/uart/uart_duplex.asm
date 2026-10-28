.program uart_duplex
; Full-duplex UART, 8N1, LSB first, on one SM: both directions on the PAU (NRZ), which keeps the
; bit timing of each while the program serves whichever is ready. Pins: TX = out_base,
; RX = in_base. 8 strobes a bit (PAU half-bit 4): baud = clk / (8 x CLKDIV).
; TX FIFO: bytes in bits 7:0. RX FIFO: bytes in bits 15:8. A stop bit received low raises IRQ 0.
; TX frames are back to back while the program is free; receiving a byte can leave idle gaps.
.out_pins 0
.set_pins 0 1
.in_pins 1
.pau enc=nrz baud=4

    set pins, 1             ; TX idles high
    set pindirs, 1
    set pau_ctl, tx|rx      ; RX: the first falling edge starts the bit clock
.wrap_target
poll:
    jmp pau rx_ready, rx
    jmp !pau tx_ready, poll     ; room for a frame in the TX queue
    jmp status tx_empty, poll   ; and a byte to send
    mov osr, null
    out pins, 1             ; start bit
    pull
    out pins, 8             ; the byte
    xnor osr, osr           ; all 1s
    out pins, 1             ; stop bit
    jmp poll
rx:
    in pins, 1              ; ISR shifts right: the bit is bit 15
    mov x, isr
    jmp x 15, poll          ; a 1: the line is idle
    mov isr, null           ; a start bit
    in pins, 8              ; the byte, bits 15:8
    push
    in pins, 1              ; the stop bit
    mov x, isr
    jmp x 15, poll
    irq 0                   ; framing error
.wrap
