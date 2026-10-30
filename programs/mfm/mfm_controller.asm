.program mfm_controller
; A floppy-style disk controller in MFM (the Miller code), a cell (bit) of 2H = 8 strobes, MSB
; first. A sector on the disk: 0x00 bytes, the address mark 0xA1, the data, their CRC-16/CCITT,
; then a gap. One SM, two pages: the dispatcher and the reader in page 0, the writer in page 1,
; a JMP FAR between them.
; TX FIFO: a command word, n - 1 in bits 14:0, bit 15 set to write: then n words, a data byte in
; bits 15:8 each. A read pushes the n data bytes and the two CRC bytes (bits 7:0) found after
; the next address mark, then STATUS (bit 12: the CRC is good).
.out_pins 0              ; write data
.in_pins 1               ; read data
.pau code=mfm baud=4
.crc width=16 poly=0x1021 init=ones
.shift_out left
.shift_in left

command:
    pull block              ; the command stays in the OSR
    mov x, osr
    jmp x 15, write         ; to the writer's page
    mov isr, null           ; Y = 0x00A1: 8 zeros, then the address mark
    set y, 0b1010
    in y, 4
    set y, 0b0001
    in y, 4
    mov y, isr
    mov isr, null
    set pau_ctl, rx|crc
hunt:                       ; the sync hunt: the last 16 bits decoded, in the ISR
    in pins, 1
    pau crc_reset           ; after each bit hunted: the CRC starts after the address mark
    mov x, isr
    jmp x!=y, hunt
    mov isr, null
    mov x, osr
    set y, 2                ; n + 2 bytes with the CRC
    add x, y
read:
    in pins, 8
    push block
    jmp x--, read
    mov isr, status         ; CRC == CRC_RES = 0: STATUS bit 12
    push block
    set pau_ctl, 0
    jmp command
write:
    jmp far, writer

.page 1
writer:
    mov osr, x
    out null, 1
    out x, 15               ; n - 1
    set pins, 0
    set pindirs, 1
    set pau_ctl, tx
    set y, 3                ; 4 bytes of 0x00
zeros:
    mov osr, null
    out pins, 8
    jmp y--, zeros
    set y, 5                ; the address mark 0xA1: 10100001
    mov osr, y              ; 101 ...
    out null, 13
    out pins, 3
    mov osr, null
    out pins, 4             ; ... 0000
    set y, 1
    mov osr, y
    out null, 15
    out pins, 1             ; ... 1
    pau crc_reset
    set pau_ctl, tx|crc
data:
    pull block
    out pins, 8
    jmp x--, data
    set pau_ctl, tx
    pau crc_append
    mov osr, null
    out pins, 8             ; a gap byte
    set pau_ctl, 0          ; drain
    wait 1 pau tx_idle
    jmp far, command
