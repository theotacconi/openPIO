.program swd
; SWD host (ARM ADIv5). SWCLK = sideset_base, SWDIO = set_base = out_base = in_base.
; PIN_MASK: SWCLK, SWDIO. ISR and OSR shift right (LSB first). 4 strobes a cycle.
; As the Raspberry Pi picoprobe: the host changes SWDIO with SWCLK low and the target samples on
; the rising edge; the target changes SWDIO after a rising edge and the host samples it at the end
; of the next high phase (SWCLK to the pin, the target and the input synchronizer take 4 clocks).
; TX FIFO: request byte (bits 7:0: start, APnDP, RnW, A2, A3, parity, stop, park); for a write,
;   then the data (2 words, LSB first). The program sends its even parity (CRC feed: a 1-bit CRC).
; RX FIFO: ACK (bits 15:13, OK = 0b001); for a read with ACK OK, data (2 words) and parity (bit 15).
; After a WAIT / FAULT ACK the data phase is skipped (write data is taken from the FIFO and dropped).
.sideset_pins 0          ; SWCLK
.set_pins 1 1            ; SWDIO
.out_pins 1
.in_pins 1
.pin_mask 0, 1
.side_set 1 opt
.crc width=1 poly=1

    set y, 0
    not y
    mov pindirs, y              ; SWCLK, SWDIO outputs
.wrap_target
    pull block side 0
    mov y, osr                  ; RnW is bit 2
    set x, 7
req:
    out pins, 1 side 0 [1]
    jmp x--, req side 1 [1]
    set pindirs, 0 side 0       ; turnaround: release SWDIO
    out null, 8 side 0          ; and empty the OSR (the data words come by autopull)
    nop side 1 [1]
    set x, 2
ack:
    nop side 0 [1]
    nop side 1
    in pins, 1 side 1       ; ACK bits
    jmp x--, ack
    mov x, isr
    push
    jmp !x 13, fail             ; ACK bit 0 = 0: WAIT or FAULT
    jmp y 2, read
    nop side 0 [1]              ; write: turnaround
    nop side 1 [1]
    set pindirs, 1 side 0
    pau crc_reset
    set x, 1                    ; two data words
word:
    pull block
    mov isr, osr
    pau crc_feed, 16            ; the word into the parity
    set y, 15
wr:
    out pins, 1 side 0 [1]
    jmp y--, wr side 1 [1]
    jmp x--, word side 0
    jmp pau crc_ok, even
    set pins, 1 side 0 [1]      ; parity 1
    jmp pend
even:
    set pins, 0 side 0 [1]      ; parity 0
pend:
    nop side 1 [1]
    mov isr, null               ; drop the copy
    jmp idle
read:
    set x, 31
rd:
    nop side 0 [1]
    nop side 1
    in pins, 1 auto side 1  ; data, pushed every 16 bits
    jmp x--, rd
    nop side 0 [1]
    nop side 1
    in pins, 1 side 1       ; parity
    push
    nop side 0 [1]              ; turnaround
    nop side 1 [1]
    set pindirs, 1 side 0
idle:
    set pins, 0 side 0 [1]      ; one idle cycle, SWDIO low
    nop side 1 [1]
.wrap
fail:
    nop side 0 [1]              ; turnaround
    nop side 1 [1]
    set pindirs, 1 side 0
    jmp y 2, idle
    pull block                  ; drop the write data
    pull block
    jmp idle
