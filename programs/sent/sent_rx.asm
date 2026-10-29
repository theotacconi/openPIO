.program sent_rx
; SENT (SAE J2716) receiver, fast channel: a sensor's frames on pin in_base. Every pulse starts
; with a falling edge; its length in ticks is the information: sync 56, nibbles 12 + value
; (status, data, CRC). The tick is the sensor's clock (+-20 %), so the program measures it on
; every sync pulse: a nibble is round(56 P / S) - 12, P its length and S the last sync's, both in
; strobes (IN TIME). No divider: 56 P is shifts and adds, the division a 5-step restoring one
; (a quotient bit a step from the sign of X, ISR full after 5 bits ends it). A pulse is a sync
; when 56 P >= 32 S. Ticks of 8 to 12 strobes (CLKDIV 15: 3 us ticks from 50 MHz, +-20 %);
; no pause pulse.
; CRC4 x^4 + x^3 + x^2 + 1 on the PAU (CRC feed), over the data nibbles and the CRC nibble:
; CRC == 0 after it means the frame is good. The specification's seed 0101 (and its extra zero
; nibble) is a prefix nibble 5 fed after a CRC reset to 0.
; RX FIFO: a sync gives 8 S (> 31); each nibble a word with the value in bits 3:0 and bit 4 =
; CRC == 0, the frame's verdict on its CRC nibble.
; Registers: X work, Y = 16 S + 1 (its bit 0 is the quotient's 1), OSR the last edge's time.
.in_pins 0
.shift_in left
.push_thresh 5
.clkdiv 15
.crc width=4 poly=0xD
.y 8961                         ; 16 x 560 + 1: a nominal sync until the first one is measured

edge:
    wait fall in_base 0
    in time, 16
    mov x, isr
    sub x, osr                  ; P
    mov osr, isr
    add x, x
    add x, x
    add x, x                    ; 8 P
    mov isr, x
    add x, x
    add x, x
    add x, x
    sub x, isr                  ; 56 P
    sub x, y
    sub x, y
    jmp x 15, nibble            ; 56 P < 32 S
    mov y, isr                  ; a sync: S = P
    add y, y
    set x, 1
    add y, x                    ; 16 S + 1
    push noblock                ; 8 S
    irq 7                       ; the next nibble is the status
    jmp edge
nibble:
    add x, y                    ; 56 P - 16 S: the first step's subtraction
    mov isr, null
    jmp test
step:
    sub x, y
test:
    jmp x 15, zero
    in y, 1                     ; quotient bit 1
    jmp next
zero:
    add x, y
    in null, 1
next:
    add x, x
    jmp !status isr_full, step  ; 5 bits: q = floor(56 P / S), 12..27
    sub x, y                    ; the remainder >= S / 2: round up
    jmp x 15, down
    mov x, isr
    xnor x, null
    jmp x--, plus4              ; ~q - 1 (never 0)
down:
    mov x, isr
    xnor x, null
plus4:
    jmp x--, p1
p1:
    jmp x--, p2
p2:
    jmp x--, p3
p3:
    jmp x--, p4
p4:
    xnor x, null                ; q + 4 (+ 1): its low 4 bits are the value
    mov isr, null
    in x, 4
    pau crc_feed, 4
    rev isr, status             ; CRC == 0 (STATUS bit 13) to bit 2
    in null, 13                 ; ... alone, to bit 15
    rev isr, isr                ; ... to bit 0
    in x, 4                     ; ... to bit 4, the value below it
    push noblock
    jmp !irq 7, edge
    irq clear 7                 ; that was the status nibble: the data start, CRC from 5
    pau crc_reset
    mov isr, null
    set x, 5
    in x, 4
    pau crc_feed, 4
    jmp edge
