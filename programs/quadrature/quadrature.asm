.program quadrature
; Quadrature encoder counter (x4: every edge of A and B counts). X = position, 16 bits, read
; by the host at any time (register X). +1 when A leads: (A, B) 00 -> 10 -> 11 -> 01 -> 00.
; Two lines changing between two samples is a missed step: IRQ 0 (not counted).
; The ISR keeps the last two samples: bits 3:2 the old (B, A), bits 1:0 the new; OUT PC jumps
; to entry old * 4 + new of the table at 0..15. A sample every 5 or 6 strobes: steps must be
; at least 8 strobes apart (2 clocks of input synchronizer included).
.in_pins 0               ; A = 0, B = 1
.shift_in left           ; new sample into bits 1:0
.shift_out right         ; OUT PC takes bits 3:0
.y 1
.start start

    jmp sample           ; 00 -> 00
    jmp up               ; 00 -> 01 (A rose)
    jmp down             ; 00 -> 10 (B rose)
    jmp missed           ; 00 -> 11
    jmp down             ; 01 -> 00
    jmp sample           ; 01 -> 01
    jmp missed           ; 01 -> 10
    jmp up               ; 01 -> 11
    jmp up               ; 10 -> 00
    jmp missed           ; 10 -> 01
    jmp sample           ; 10 -> 10
    jmp down             ; 10 -> 11
    jmp missed           ; 11 -> 00
    jmp down             ; 11 -> 01
    jmp up               ; 11 -> 10
    jmp sample           ; 11 -> 11
start:
    in pins, 2           ; the first sample is the old one of the next
    jmp sample
missed:
    irq 0
    jmp sample
down:
    sub x, y
    jmp sample
up:
    add x, y
sample:
    in pins, 2
    mov osr, isr
    out pc, 4
