.program hdlc_tx
; HDLC frame transmitter on an NRZI line (a 0 is a transition), as AX.25 packet radio. The PAU
; does NRZI, zero insertion (a 0 after five 1s, in its own bit time) and the FCS (CRC-16/X.25);
; the program frames: flags 0x7E sent with stuffing off. 8 strobes a bit (PAU half-bit 4).
; TX FIFO: N (bytes in the frame), then the N bytes, one a word (bits 7:0), LSB first on the
; line. Between frames the line holds its level (all 1s: idle).
.pau enc=nrzi stuff=5 baud=4
.crc width=16 poly=0x1021 init=ones xorout=1 res=0x1D0F
.out_pins 0
.set_pins 0 1
.y 0x7E                         ; the flag

    set pins, 1
    set pindirs, 1
    set pau_ctl, tx
.wrap_target
    pull block                  ; N
    out x, 16
    pau crc_reset
    mov osr, y
    out pins, 8                 ; opening flag, not stuffed
    set pau_ctl, tx|stuff|crc
    jmp next
byte:
    pull block
    out pins, 8
next:
    jmp x--, byte
    set pau_ctl, tx|stuff       ; the FCS: stuffed, not fed to the CRC
    pau crc_append
    mov osr, y
    set pau_ctl, tx
    out pins, 8                 ; closing flag
.wrap
