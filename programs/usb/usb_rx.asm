.program usb_rx
; USB low-speed receiver. Pins: D- = in_base, D+ = in_base + 1. 8 strobes a bit (PAU half-bit 4).
; Every byte of a packet goes to the RX FIFO (bits 15:8) as it arrives: SYNC, PID, payload, CRC.
; At the end of packet (SE0) IRQ 0 if the CRC16 of the payload is right, IRQ 1 if not (also for
; packets without a CRC16: tokens, handshakes).
.pau enc=nrzi diff stuff=6 baud=4
.crc width=16 poly=0x8005 init=ones xorout=1 res=0x800D
.push_thresh 8

.wrap_target
    set pau_ctl, 0
    wait 0 pau se0          ; idle J
    set pau_ctl, rx|stuff   ; the first K edge of SYNC starts the bit clock
    in pins, 8 auto         ; SYNC
    in pins, 8 auto         ; PID
    pau crc_reset
    set pau_ctl, rx|stuff|crc
bit:
    jmp pau rx_ready, take
    jmp !pau se0, bit       ; until SE0 (DIFF: it decodes no bit)
    jmp pau crc_ok, good
    irq 1
.wrap
good:
    irq 0
    jmp 0
take:
    in pins, 1 auto         ; pushed every 8 bits
    jmp bit
