.program usb_tx
; USB low-speed data packet transmitter. Pins: D- = out_base = set_base, D+ = D- + 1 (set_count 2).
; 8 strobes a bit (PAU half-bit 4): 1.5 Mbit/s at 12 MHz strobes. The PAU does NRZI, bit stuffing
; (a 0 after six 1s, in its own bit time) and the CRC16; the program frames the packet.
; TX FIFO: SYNC (bits 7:0, 0x80) | PID (bits 15:8); payload length N; then N bytes (bits 7:0).
; IRQ 0 after each packet.
.out_pins 0              ; D-, D+
.set_pins 0 2
.pau enc=nrzi diff stuff=6 baud=4
.crc width=16 poly=0x8005 init=ones xorout=1 res=0x800D

    set pins, 1             ; J idle (D- high)
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
    set pau_ctl, 0          ; drain: SE0 starts right after the last bit
    wait 1 pau tx_idle [15] ; SE0 for 2 bit times
    set pins, 1 [7]         ; J for 1 bit time
    irq 0
.wrap
