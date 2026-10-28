.program dali_master
; DALI (IEC 62386-101) bus master through an inverting transceiver: pin out_base high pulls the
; bus low, pin in_base reads the bus inverted; INV makes the PAU work with bus levels.
; Manchester, 1 = the bus rising in the middle of the bit; half-bit Te = 8 strobes: 1200 baud
; from 50 MHz is CLKDIV 2604 + 43 / 256.
; TX FIFO: one forward frame a word (address byte, data byte): start bit, 16 bits MSB first,
; then the bus idles high (stop bits). RX FIFO: one word a forward frame: 0x0100 | the answer
; for a backward frame starting within 248 strobes (12.9 ms) of the forward frame's last bit,
; 0 for no answer. A backward frame that is not start bit + 8 bits (two gear answering at once)
; raises IRQ 0 and is pushed as far as it was read.
.out_pins 0
.set_pins 0 1
.in_pins 1
.pau enc=manchester inv baud=8
.shift_out left
.shift_in left

    set pins, 0             ; the bus idles high
    set pindirs, 1
    set timeout_val, 31
.wrap_target
    pull block
    mov x, osr
    set y, 1
    rev osr, y              ; 0x8000: the start bit
    set pau_ctl, tx         ; the encoder starts from the pin: bus high
    out pins, 1
    mov osr, x
    out pins, 16            ; 17 bits queued (the queue holds 32)
    set pau_ctl, 0          ; drain: the pin returns to the latch after the last bit
    wait 1 pau tx_idle
    set dlymul, 8
    wait 1 in_base 0 timeout    ; bus low: a backward frame starts; or 31 x 8 strobes
    set dlymul, 1
    jmp timeout, none
    set pau_ctl, rx         ; before the start bit's middle: its rising edge is the first one
    set x, 8
bit:
    wait 1 pau rx_ready timeout ; the next bit within 31 strobes (2 bit times)
    jmp timeout, short
    in pins, 1
    jmp x--, bit
    wait 1 pau rx_ready timeout ; nothing more: the stop bits
    jmp timeout, done
short:
    irq 0
done:
    set pau_ctl, 0
none:
    push                    ; ISR: 0x0100 | answer, or 0 (cleared by the last push)
.wrap
