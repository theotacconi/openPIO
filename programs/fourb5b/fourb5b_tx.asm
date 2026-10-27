.program fourb5b_tx
; 4b5b encoding in software, biphase mark (BMC) line code by the PAU: the USB Power Delivery
; physical layer. Pin: out_base = set_base. 8 strobes a bit (PAU half-bit 4).
; A 32-word jump table turns nibble n into its 5-bit symbol: OUT PC jumps to entry 2n.
; Host: PC = start (SM stopped). TX FIFO: one nibble a word, as 2n in bits 4:0.
; Symbols leave LSB first.
.pau enc=bmc baud=4

    set y, 0b11110        ; 0
    jmp emit
    set y, 0b01001        ; 1
    jmp emit
    set y, 0b10100        ; 2
    jmp emit
    set y, 0b10101        ; 3
    jmp emit
    set y, 0b01010        ; 4
    jmp emit
    set y, 0b01011        ; 5
    jmp emit
    set y, 0b01110        ; 6
    jmp emit
    set y, 0b01111        ; 7
    jmp emit
    set y, 0b10010        ; 8
    jmp emit
    set y, 0b10011        ; 9
    jmp emit
    set y, 0b10110        ; A
    jmp emit
    set y, 0b10111        ; B
    jmp emit
    set y, 0b11010        ; C
    jmp emit
    set y, 0b11011        ; D
    jmp emit
    set y, 0b11100        ; E
    jmp emit
    set y, 0b11101        ; F
    jmp emit
start:
    set pins, 0
    set pindirs, 1
    set pau_ctl, tx
.wrap_target
    pull block
    out pc, 5               ; to entry 2n
emit:
    mov osr, y
    out pins, 5             ; the symbol, through the PAU
.wrap
