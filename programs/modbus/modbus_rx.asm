.program modbus_rx
; Modbus RTU receiver: 11-bit characters (start, 8 data bits LSB first, 2 stop bits: RTU without
; parity), a frame ends after 3.5 characters of silence, CRC-16/MODBUS checked on the chip.
; Pin: in_base (RX). 8 strobes a bit: baud = clk / (8 x CLKDIV); 9600 baud from 50 MHz is CLKDIV
; 651 + 11 / 256. Cycles below count from a start edge, seen at cycle 1-2.
; RX FIFO: each byte in bits 15:8 (low byte 0), then 0x0001 after a frame with a good CRC,
; 0x0003 after one with a bad CRC. A low stop bit raises IRQ 0.
.in_pins 0
.crc width=16 poly=0x8005 init=ones     ; bits LSB first: CRC-16/MODBUS, 0 after a good CRC

    set timeout_val, 29     ; x DLYMUL 11: 319 strobes from cycle 78, 3.5 characters from the end
    wait 1 in_base 0        ; an idle line first
.wrap_target
    pau crc_reset
    wait 0 in_base 0        ; the first start bit: no timeout
    jmp byte [9]
next:
    set dlymul, 11
    wait 0 in_base 0 timeout    ; the next start bit, or 3.5 characters of silence
    set dlymul, 1
    jmp timeout, end [8]
byte:
    set x, 6                ; cycle 11
bit:
    in pins, 1 [6]          ; cycles 12, 20, ... 60: the middle of bits 0..6
    jmp x--, bit
    in pins, 1              ; cycle 68: bit 7
    pau crc_feed 8
    push [5]                ; the byte, bits 15:8
    jmp in_base 0, next     ; cycle 76: the middle of the first stop bit
    irq 0                   ; framing error
    wait 1 in_base 0
    jmp next
end:
    set x, 1
    jmp pau crc_zero, mark
    set x, 3
mark:
    mov isr, x
    push
.wrap
