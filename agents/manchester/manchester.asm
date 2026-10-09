.program manchester_tx
; Manchester / 10BASE-T Transmitter
; Pin 0: TX (Differential / Single-ended Manchester line)
; PAU Hardware Engine: pau_cfg bit 3 (manchester_tx), pau_baud = 3
; Bit 0: Low-to-High transition in middle of bit cell
; Bit 1: High-to-Low transition in middle of bit cell

    set pau_baud, 3     ; 3 machine cycles per half-period (6 cycles/bit)
    set pau_cfg, 8      ; Enable PAU Manchester TX engine (Bit 3)

.wrap_target
    pull block          ; Fetch byte payload from TX FIFO
    set x, 7            ; 8 bits to serialize

bit_loop:
    out pins, 1         ; PAU hardware modulates bit into two half-periods
    jmp x--, bit_loop   ; Loop for all 8 bits

    irq 0               ; Frame complete IRQ 0
    push noblock        ; Acknowledge frame completion
.wrap
