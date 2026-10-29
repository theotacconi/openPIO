.program mil1553_bc
; MIL-STD-1553B bus controller on a bus transceiver: TX = pin 0, TXB = pin 1 (its complement),
; TXINH = pin 2 (high: transmitter off), RX = pin 3 (the bus, high on its positive half).
; 1 Mbit/s Manchester II from a 50 MHz clock: a half-bit is 25 strobes. A word is a sync of
; 3 bit times (1.5 high then 1.5 low for a command or status word, the opposite for data),
; 16 bits MSB first (1: high then low) and odd parity.
; Sending is software: exact half-bits from SET and delays, the parity a 1-bit CRC fed a bit at
; a time (the sync is not Manchester, so not the PAU). Receiving is the PAU's Manchester
; decoder (INV: 1553's 1 falls in the middle), armed after the sync is found in software, the
; parity a 1-bit CRC on the decoded bits.
; TX FIFO, a message: words to send - 1, the command word, its data words (sent back to back,
;   so loaded at once), then words to receive - 1 (the status word and its data words).
; RX FIFO: per received word, the word, then STATUS (bit 12: odd parity); at the end Y: 0xFFFF
;   when every word came, else the words missing - 1 (no response within 14 us).
.set_pins 0 3                   ; TX, TXB, TXINH
.in_pins 3
.pin_mask 0, 1, 2
.shift_out left
.shift_in left
.pull_thresh 16
.pau enc=manchester inv baud=25
.crc width=1 poly=1 res=1       ; CRC == CRC_RES: the XOR of the bits is 1

    set pins, 4                 ; transmitter off
    set pindirs, 7
msg:
    set dlymul, 1
    pull
    mov y, osr                  ; words to send - 1
    pull                        ; the command word
    set pins, 1 [31]            ; command sync: high 1.5 bits
    pau crc_reset [31]
    nop [10]
    set pins, 2 [31]            ; low 1.5 bits
    nop [31]
    nop [8]
bit:
    out x, 1
    jmp x 0, one
    set pins, 2 [22]            ; 0: low, then high
    in x, 1
    pau crc_feed, 1
    set pins, 1 [20]
    jmp tail
one:
    set pins, 1 [22]            ; 1: high, then low
    in x, 1
    pau crc_feed, 1
    set pins, 2 [21]
tail:
    jmp !status osr_empty, bit
    jmp pau crc_ok, pzero [1]   ; odd parity: 0 when the XOR is 1
    set pins, 1 [24]
    set pins, 2 [21]
    jmp pdone
pzero:
    set pins, 2 [24]
    set pins, 1 [22]
pdone:
    jmp y--, more
    nop
    set pins, 4                 ; transmitter off at the end of the parity bit
    pull
    mov y, osr                  ; words to receive - 1
    set dlymul, 28              ; delays x 28 from here
    set timeout, 25             ; 700 strobes: no response by 14 us
    wait 1 in_base 0 timeout
    jmp in_base 0, status
    jmp done
more:
    pull                        ; the next data word, back to back
    set pins, 2 [31]            ; data sync: low 1.5 bits
    pau crc_reset [31]
    nop [10]
    set pins, 1 [31]            ; high 1.5 bits
    nop [31]
    nop [7]
    jmp bit
status:
    wait fall in_base 0         ; the middle of a status sync
    jmp word
dword:
    wait rise in_base 0         ; the middle of a data sync
word:
    pau crc_reset [3]           ; 1.75 bits after it (84 strobes): between the first bit's edges
    set pau_ctl, 10             ; RX, CRC on
    in pins, 16
    push
    in pins, 1                  ; parity
    set pau_ctl, 0
    mov isr, status
    push
    jmp y--, dword
done:
    mov isr, y
    push
    jmp msg
