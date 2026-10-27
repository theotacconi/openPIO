.program swd
; SWD host (ARM ADIv5). SWCLK = sideset_base, SWDIO = set_base = out_base = in_base.
; PIN_MASK: SWCLK, SWDIO. ISR and OSR shift right (LSB first). 4 strobes a cycle.
; As the Raspberry Pi picoprobe: the host changes SWDIO with SWCLK low and the target samples on
; the rising edge; the target changes SWDIO after a rising edge and the host samples on the next.
; TX FIFO: request byte (bits 7:0: start, APnDP, RnW, A2, A3, parity, stop, park); for a write,
;   then the data (2 words, LSB first) and its even parity (bit 0), computed by the host.
; RX FIFO: ACK (bits 15:13, OK = 0b001); for a read with ACK OK, data (2 words) and parity (bit 15).
; After a WAIT / FAULT ACK the data phase is skipped (write data is taken from the FIFO and dropped).
.side_set 1 opt

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
    in pins, 1 side 1 [1]       ; ACK bits
    jmp x--, ack
    mov x, isr
    push
    jmp !x 13, fail             ; ACK bit 0 = 0: WAIT or FAULT
    jmp y 2, read
    nop side 0 [1]              ; write: turnaround
    nop side 1 [1]
    set pindirs, 1 side 0
    set x, 31
wr:
    out pins, 1 auto side 0 [1] ; data, then the parity bit of the next word
    jmp x--, wr side 1 [1]
    out pins, 1 auto side 0 [1]
    nop side 1 [1]
    out null, 15 side 0         ; rest of the parity word
    jmp idle
read:
    set x, 31
rd:
    nop side 0 [1]
    in pins, 1 auto side 1 [1]  ; data, pushed every 16 bits
    jmp x--, rd
    nop side 0 [1]
    in pins, 1 side 1 [1]       ; parity
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
    pull block                  ; drop the write data and parity
    pull block
    pull block
    jmp idle
