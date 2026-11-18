.program lin_master
; LIN master (commander). TX = set_base = out_base (to the transceiver), RX = in_base (the bus).
; 8 strobes a bit: baud = strobes / 8. OSR and ISR shift right (LSB first).
; TX FIFO: header word: [7:0] protected ID (ID and parity, from the host), [11:8] n = response
;   bytes (data + checksum), [15] 1: the master sends the response, whose n bytes follow, one a
;   word (bits 7:0); 0: a slave sends it and the program receives it.
; RX FIFO: the received bytes (bits 15:8). The host computes the checksum (a sum with carry).
; A byte is sent by a subroutine: return address in Y, back with OUT PC.
.set_pins 0 1            ; TX
.out_pins 0
.in_pins 1               ; RX
.pull_thresh 8
.push_thresh 8

    set pins, 1             ; recessive
    set pindirs, 1
    jmp start
; the bytes after the header first: their labels are return addresses (SET: 0..31)
pbyte:
    pull block
    set y, pnext
    jmp send
pnext:
    jmp x--, pbyte
    jmp start
rbyte:
    wait 0 in_base 0 [10]   ; start edge, then the middle of bit 0
    set y, 7
rbit:
    in pins, 1 auto [6]     ; a byte pushed with its 8th bit
    jmp y--, rbit
    jmp in_base 0, rnext    ; the stop bit high: on to the next start bit at once (a slave 2 %
    wait 1 in_base 0        ; fast starts it a strobe and a half early); low: wait for idle
rnext:
    jmp x--, rbyte
    jmp start
start:
    pull block
    mov isr, osr            ; keep the header
    set dlymul, 4
    set pins, 0 [25]        ; break: 1 + 25 x 4 strobes
    set dlymul, 1 [2]       ; 104 strobes = 13 bits
    set pins, 1 [7]         ; break delimiter (2 bits with what follows)
    set x, 21               ; sync 0x55 = 21 x 4 + 1
    add x, x
    add x, x
    set y, 1
    add x, y
    mov osr, x
    set y, pid
    jmp send
pid:                        ; the response decided before the protected ID goes out, so that
    mov osr, isr            ; the program waits for a slave's start bit before the ID's stop
    out null, 8             ; bit ends: a response may follow the header at once
    out x, 4                ; n
    out y, 4                ; bit 3: the master sends the response
    mov osr, isr            ; protected ID: header bits 7:0
    jmp !y 3, pid_rx
    set y, pnext
    jmp send
pid_rx:
    mov isr, null
    set y, rnext
    jmp send
send:                       ; the byte in the OSR, LSB first; return address in Y
    set pins, 0 [7]         ; start bit
bit:
    out pins, 1 [6]
    jmp !osre, bit
    set pins, 1 [2]         ; stop bit: 8 strobes or more with what follows (a slave's start
    mov osr, y              ; bit is waited for from strobe 6)
    out pc, 6               ; return
