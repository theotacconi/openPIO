.program uart_autobaud
; UART receiver, 8N1, that learns the baud rate from the first character, 0x55 ('U', as LIN's
; sync byte): from its start bit's falling edge to its fifth falling edge are 8 bit times,
; measured with IN TIME; their sixteenth, rounded, is the PAU half-bit (OUT PAU_BAUD). Then
; every byte through the PAU (NRZ), framed in software. Pin: in_base (RX).
; Rates: a bit of 8 to 62 strobes (half-bit 4 to 31).
; RX FIFO: first the 8 bit times in strobes, then each byte in bits 15:8. A stop bit received
; low raises IRQ 0.
.in_pins 0
.pau code=nrz

    wait 1 in_base 0        ; an idle line
    wait 0 in_base 0        ; 0x55: the start bit
    nop                     ; as the jmp after the last WAIT below
    in time, 16
    mov y, isr
    set x, 3
fall:
    wait 1 in_base 0
    wait 0 in_base 0        ; falling edges 2..5: bits 1, 3, 5, 7
    jmp x--, fall
    in time, 16
    mov x, isr
    sub x, y                ; 8 bit times, in strobes
    mov isr, x
    push
    set y, 8
    add x, y                ; rounded: + 8 before / 16
    mov osr, x
    out null, 4
    out pau_baud, 5         ; half-bit = 8 bit times / 16
    set pau_ctl, rx         ; the bit clock starts at the next edge: bit 7 -> stop bit
.wrap_target
bit:
    in pins, 1              ; ISR shifts right: the bit is bit 15
    mov x, isr
    jmp x 15, bit           ; a 1: stop bit or idle
    mov isr, null           ; a start bit
    in pins, 8              ; the byte, bits 15:8
    push
    in pins, 1              ; the stop bit
    mov x, isr
    jmp x 15, bit
    irq 0                   ; framing error
.wrap
