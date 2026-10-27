.program lin_master
; LIN master (commander). TX = set_base = out_base (to the transceiver), RX = in_base (the bus).
; 8 strobes a bit: baud = strobes / 8. OSR and ISR shift right (LSB first).
; TX FIFO: header word: [7:0] protected ID (ID and parity, from the host), [11:8] n = response
;   bytes (data + checksum), [15] 1: the master sends the response, whose n bytes follow, one a
;   word (bits 7:0); 0: a slave sends it and the program receives it.
; RX FIFO: the received bytes (bits 15:8). The host computes the checksum (a sum with carry).
; A byte is sent by a subroutine: return address in Y, back with OUT PC.
.pull_thresh 8

    set pins, 1             ; recessive
    set pindirs, 1
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
pid:
    mov osr, isr            ; protected ID: header bits 7:0
    set y, resp
    jmp send
resp:
    mov osr, isr
    out null, 8
    out x, 4                ; n
    out y, 4                ; bit 3: the master sends the response
    mov isr, null
    jmp !y 3, receive
    jmp pnext
pbyte:
    pull block
    set y, pnext
    jmp send
pnext:
    jmp x--, pbyte
    jmp start
receive:
    jmp rnext
rbyte:
    wait 0 in_base 0 [10]   ; start edge, then the middle of bit 0
    set y, 7
rbit:
    in pins, 1 [6]
    jmp y--, rbit
    push
    wait 1 in_base 0        ; stop bit
rnext:
    jmp x--, rbyte
    jmp start
send:                       ; the byte in the OSR, LSB first; return address in Y
    set pins, 0 [7]         ; start bit
bit:
    out pins, 1 [6]
    jmp !osre, bit
    set pins, 1 [6]         ; stop bit
    mov osr, y
    out pc, 6               ; return
