.program usb_rx
; USB low-speed receiver. Pins: D- = in_base, D+ = in_base + 1. 8 strobes a bit (PAU half-bit 4).
; Every byte of a packet goes to the RX FIFO (bits 15:8) as it arrives: SYNC, PID, payload, CRC.
; At the end of packet (SE0) IRQ 0 if the CRC16 of the payload is right, IRQ 1 if not (also for
; packets without a CRC16: tokens, handshakes). SE1 (both lines high, an illegal state) raises IRQ 2
; and drops the packet up to its EOP.
.in_pins 0               ; D-, D+
.pau code=nrzi stuff=6 baud=4
.crc width=16 poly=0x8005 init=ones xorout=1 res=0x800D
.push_thresh 8

.wrap_target
    set pau_ctl, 0
    wait 0 pau line_00      ; idle J
    set pau_ctl, rx|stuff   ; the first K edge of SYNC starts the bit clock
    in pins, 8 auto         ; SYNC
    in pins, 8 auto         ; PID
    pau crc_reset
    set pau_ctl, rx|stuff|crc
    set x, 7
    mov y, null             ; no whole byte yet: no CRC verdict
bit:
    jmp pau rx_ready, take
    jmp pau line_11, illegal
    jmp !pau line_00, bit   ; until SE0
    jmp y 12, good          ; the CRC verdict at the last whole byte: SE0 decodes a bit or two
    irq 1
.wrap
illegal:
    irq 2
    wait 1 pau line_00      ; the rest of the packet, to its EOP
    jmp 0
good:
    irq 0
    jmp 0
take:
    in pins, 1
    jmp x--, bit
    push                    ; a byte
    mov y, status           ; CRC == CRC_RES (STATUS bit 12) as the byte ends
    set x, 7
    jmp bit
