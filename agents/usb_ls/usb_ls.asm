.program usb_ls_tx
; USB Low-Speed packet transmitter on the PAU (ISA v2)
; Pin 0: D-, pin 1: D+ (J = D- high, D+ low; K = D- low, D+ high; SE0 = both low)
; 4 strobes per bit (PAU half-bit 2). The PAU does NRZI, bit stuffing (after six 1s, in its own
; bit time) and the CRC16 of the payload; the program only frames the packet.
; TX FIFO: word 0 = SYNC (bits 7:0, 0x80) | PID (bits 15:8); word 1 = payload length N;
;          then N words, one payload byte each (bits 7:0). Sent LSB first.
.pau enc=nrzi diff stuff=6 baud=2
.crc width=16 poly=0x8005 init=ones xorout=1 res=0x800D

    set pins, 1             ; J idle
    set pindirs, 3
.wrap_target
    pull block              ; SYNC + PID
    set pau_ctl, tx|stuff   ; NRZI from the J on the pins
    out pins, 16
    pull block
    out y, 16               ; payload length
    pau crc_reset
    set pau_ctl, tx|stuff|crc
    jmp next
byte:
    pull block
    out pins, 8
next:
    jmp y--, byte
    pau crc_append          ; CRC16, complemented, stuffed like data
    set pins, 0             ; SE0 waits under the PAU
    set pau_ctl, 0          ; drain: SE0 right after the last bit
    wait 1 pau tx_idle [7]  ; SE0 for 2 bit times
    set pins, 1 [3]         ; J for 1 bit time
    irq 0                   ; packet sent
.wrap
