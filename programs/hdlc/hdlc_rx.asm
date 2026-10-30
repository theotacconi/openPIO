.program hdlc_rx
; HDLC frame receiver on an NRZI line (pairs with hdlc_tx). The PAU does NRZI, removes the
; inserted zeros and computes the FCS (CRC-16/X.25); the program finds the flags. 8 strobes a
; bit (PAU half-bit 4).
; A flag is the one byte with six 1s: it sets stuff_err (and arrives whole). Hunting, the program
; compares the last 8 bits with a flag; in a frame it reads bytes until one sets stuff_err. The
; CRC then holds 0x53C5: CRC-16/X.25's residue 0x1D0F after the FCS, advanced by the flag's bits.
; RX FIFO: the frame's bytes, FCS included, one a word (bits 15:8, bits 7:0 = 0); then an end
; word 0x7E01 (FCS wrong) or 0x7E03 (right). Flags back to back (empty frames) give nothing.
.pau code=nrzi stuff=5 baud=4
.crc width=16 poly=0x1021 init=ones xorout=1 res=0x53C5
.in_pins 0
.shift_in right
.x 0xFF00                       ; mask of the last 8 bits, moved to the OSR
.y 0x7E00                       ; a flag, as it lands in the ISR

    mov osr, x
    set pau_ctl, rx|stuff|crc
hunt:                           ; 4 strobes a bit
    in pins, 1
    mov x, isr
    and x, osr
    jmp x!=y, hunt
frame:
    pau crc_reset               ; CRC and stuff_err for this frame
    mov isr, null
    in pins, 8
    jmp !pau stuff_err, data
    mov x, isr
    jmp x!=y, hunt              ; six 1s, not a flag: abort or idle
    jmp frame                   ; another flag: an empty frame
data:
    push                        ; a byte
    in pins, 8
    jmp !pau stuff_err, data
    mov x, isr
    jmp x!=y, hunt
    set x, 1                    ; a flag: end of frame, checked 5 strobes after its last bit,
    jmp !pau crc_ok, end        ; before the next bit feeds the CRC
    set x, 3
end:
    or isr, x
    push
    jmp frame
