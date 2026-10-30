.program onewire
; 1-Wire master, standard speed. DQ = set_base = in_base, in the chip open-drain mask, pulled up;
; PIN_MASK: DQ. 1 strobe = 1 us (CLKDIV = clock in MHz). ISR shifts right (LSB first).
; TX FIFO, one command a word: bit 15 set = reset pulse; else bits 7:0 = a byte to write, LSB
; first (0xFF reads a byte: a read slot is a write-1 slot sampled at 15 us); bit 14 resets the
; CRC8 first.
; RX FIFO, one result a command: bits 15:8 the 8 bits seen on DQ, which feed the CRC8, and bit 0
; set when the CRC8 is then 0 (a block read with its CRC is right). After a reset, bit 15 is the
; presence (0: a device answered).
.set_pins 4 1            ; DQ
.in_pins 4
.open_drain 4
.pin_mask 4
.crc width=8 poly=0x31

    set pins, 1
    set pindirs, 1
.wrap_target
    pull block
    mov x, osr
    jmp x 15, reset
    jmp !x 14, keep
    pau crc_reset
keep:
    set x, 7
bit:                        ; 70 us a slot, t0 = slot start
    out y, 1
    set pins, 0 [5]         ; t0: low
    jmp !y, zero
    set pins, 1 [7]         ; t0 + 7: release (write 1, or read)
    in pins, 1 [31]         ; t0 + 15: sample
    nop [21]
    jmp x--, bit            ; t0 + 69
    jmp byte
zero:
    nop [7]
    in pins, 1 [31]         ; t0 + 15: reads 0
    nop [12]
    set pins, 1 [8]         ; t0 + 60: release
    jmp x--, bit            ; t0 + 69
byte:
    pau crc_feed, 8         ; the byte, LSB first (its order on DQ)
    mov x, isr
    jmp !pau crc_ok, result
    set y, 1
    or x, y                 ; bit 0: CRC8 is 0
result:
    mov isr, x
    jmp done
reset:
    set dlymul, 16
    set pins, 0 [30]        ; low 481 us
    set dlymul, 1
    set pins, 1 [31]        ; release at T
    nop [31]
    nop [5]
    in pins, 1              ; T + 70: presence
    set dlymul, 16
    nop [26]                ; recovery, 480 us after the release
    set dlymul, 1
done:
    push
.wrap
