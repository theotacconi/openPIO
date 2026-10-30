.program sd_command
; SD card commands in SPI mode, sent through the PAU: the bit clock on the PAU's second pin
; (SCK), the CRC-7 appended by the PAU, then the end bit. Mode 0 for an SD card; the clock's
; CPOL and CPHA (PAU_MODE) give any other mode, the program does not change.
; TX FIFO: a command a burst of 5 words, a byte in bits 15:8 each: 0x40 | index, then the
; 32-bit argument MSB first. On the line: CS_N low, the 5 bytes, CRC-7, 1, CS_N high.
.out_pins 0              ; MOSI; SCK on pin 1, the PAU's second pin
.set_pins 0 3            ; MOSI, SCK, CS_N
.pau code=nrz pin2=clock baud=4
.crc width=7 poly=0x09
.shift_out left

    set pins, 0b100         ; CS_N high
    set pindirs, 0b111
.wrap_target
    pull block              ; the first byte
    set pau_ctl, tx         ; SCK at CPOL
    set pins, 0             ; CS_N low (the PAU drives MOSI and SCK)
    pau crc_reset
    set pau_ctl, tx|crc
    out pins, 8
    set x, 3
arg:
    pull block
    out pins, 8
    jmp x--, arg
    set pau_ctl, tx
    pau crc_append          ; CRC-7 of the 40 bits
    set y, 1
    mov osr, y
    out null, 15
    out pins, 1             ; the end bit
    wait 1 pau tx_idle      ; the last bit sent, SCK back at CPOL
    set pins, 0b100         ; CS_N high, then the pins return to the latches
    set pau_ctl, 0
.wrap
