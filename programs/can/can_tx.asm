.program can_tx
; CAN 2.0A frame transmitter. TX = out_base = set_base, RX = in_base (the transceiver output).
; 8 strobes a bit (PAU half-bit 4). The PAU stuffs (after 5 equal bits), computes the CRC15 and
; monitors every bit up to the CRC delimiter: on a lost arbitration or a bit error it goes quiet
; at once. The host starts a frame on an idle bus.
; TX FIFO: N = bits from SOF to the end of the data field, then those bits MSB first, 16 a word.
; Result: IRQ 0 sent and acknowledged, IRQ 1 no ACK, IRQ 2 arbitration lost or bit error.
.out_pins 0              ; TX
.set_pins 0 1
.in_pins 1               ; RX
.shift_out left
.pau enc=nrz stuff=5 stuff_mode=equal baud=4
.crc width=15 poly=0x4599

    set pins, 1             ; recessive
    set pindirs, 1
.wrap_target
    pull block
    out x, 16               ; N (and the OSR is empty: autopull takes the bits)
    pau crc_reset
    set pau_ctl, tx|stuff|crc|monitor
    jmp next
bit:
    out pins, 1 auto        ; SOF .. data
next:
    jmp x--, bit
    pau crc_append          ; CRC15, stuffed and monitored
    set pau_ctl, tx|monitor
    mov osr, null
    not osr
    out pins, 1             ; CRC delimiter (recessive), not stuffed
    set pau_ctl, 0          ; drain: the pin returns to recessive after the delimiter
    wait 1 pau tx_idle [3]  ; middle of the ACK slot (at once after a bit error)
    jmp pau bit_err, lost
    jmp in_base 0, noack    ; still recessive: nobody acknowledged
    set x, 9
eof:
    jmp x--, eof [7]        ; ACK delimiter, EOF (7 bits) and intermission: 10 bits
    irq 0
.wrap
noack:
    irq 1
    jmp 2
lost:
    irq 2
    jmp 2
