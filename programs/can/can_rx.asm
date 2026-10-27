.program can_rx
; CAN 2.0A frame receiver. RX = in_base, TX = set_base (to acknowledge).
; 8 strobes a bit (PAU half-bit 4); the PAU unstuffs and checks the CRC15.
; RX FIFO, per frame: SOF, ID, RTR, IDE, r0 (bits 14:0); DLC (bits 3:0); one data byte a word
; (bits 7:0). Then IRQ 0 and an ACK if the CRC is right, IRQ 1 if not.
.pau enc=nrz stuff=5 stuff_mode=equal baud=4
.crc width=15 poly=0x4599

    set pins, 1             ; recessive
    set pindirs, 1
.wrap_target
    set pau_ctl, 0
    wait 1 in_base 0        ; bus recessive
    pau crc_reset
    set pau_ctl, rx|stuff|crc   ; the SOF edge starts the bit clock
    in pins, 15             ; SOF, ID, RTR, IDE, r0
    push
    in pins, 4              ; DLC
    mov x, isr
    push
    jmp next
byte:
    in pins, 8
    push
next:
    jmp x--, byte
    in pins, 15             ; CRC
    set pau_ctl, rx|stuff   ; the delimiter does not feed the CRC (it comes 2 bit-halves later)
    jmp !pau crc_zero, bad
    in pins, 1              ; CRC delimiter (a stuffed bit before it is still dropped)
    set pins, 0 [7]         ; ACK slot: dominant for one bit, as soon as the delimiter is seen
    set pins, 1
    irq 0
.wrap
bad:
    irq 1
    jmp 2
