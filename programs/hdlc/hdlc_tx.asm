.program hdlc_tx
; HDLC frame transmitter on an NRZI line (a 0 is a transition), as AX.25 packet radio. The PAU
; does NRZI, zero insertion (a 0 after five 1s, in its own bit time) and the FCS (CRC-16/X.25);
; the program frames: flags 0x7E sent with stuffing off. 8 strobes a bit (PAU half-bit 4).
; TX FIFO: N (bytes in the frame), then the N bytes, one a word (bits 7:0), LSB first on the
; line. Between frames the line holds its level (all 1s: idle).
; The host cancels the frame in progress by setting IRQ 1: the queued bits are dropped at once
; (TX abort), the line holds for 9 bit times (the abort: seven 1s or more), then IRQ 0 and the
; program stops; the host loads it again (its RESET drops the frame's bytes left in the TX FIFO).
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
    jmp irq 1, abort
    pull block
    out pins, 8
next:
    jmp x--, byte
    pau crc_append              ; the FCS after the data bits, stuffed (appended bits never feed the CRC)
    mov osr, y
    set pau_ctl, tx
    out pins, 8                 ; closing flag
.wrap
abort:
    pau tx_abort                ; the line returns to the latch now: TX off
    set dlymul, 4
    nop [18]                    ; 9 bit times without a transition
    irq 0
stop:
    jmp stop
