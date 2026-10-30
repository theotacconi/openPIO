.program iso7816_reader
; ISO/IEC 7816-3 interface device (smart-card reader), character level, direct convention,
; F = 372, D = 1: one elementary time unit (ETU) = 372 card clocks = 16 strobes (CLKDIV 232.5
; with the card clock at clk / 10, iso7816_clk.asm). I/O = pin 0 (open drain, pull-up), RST =
; pin 2; CLK = pin 1 belongs to the clock SM.
; Cold reset: RST low while the clock starts, high after 400 card clocks; then the card's
; answer to reset and everything else is characters on I/O, half duplex: start bit, 8 data bits
; LSB first, even parity, 2 ETU of guard time (12 ETU a character).
; Error signalling, both ways: a receiver that finds the parity wrong pulls I/O low at 10.5 ETU
; for 1.5 ETU and the sender repeats the character; sending, the reader looks at I/O at 11 ETU
; and repeats the character after the card's error signal.
; RX FIFO: each received character, data in bits 7:0, parity in bit 8; bit 15 = parity error
; (signalled: the card repeats it). TX FIFO: characters to send (bits 7:0), when it is the
; reader's turn (T = 0: a command header, data).
.set_pins 0 3                   ; I/O, (CLK), RST
.out_pins 0
.in_pins 0
.open_drain 0
.pin_mask 0, 2
.shift_in right
.shift_out right
.pull_thresh 8
.clkdiv 232 128
.crc width=1 poly=1             ; parity: the XOR of the bits

    set pins, 1                 ; I/O released, RST low
    set pindirs, 5 [31]         ; 32 strobes: 744 card clocks (400 needed)
    set y, 1                    ; bit 0: the error flag of a pushed word
    set pins, 5                 ; RST high
idle:
    jmp !status tx_empty, send
    jmp in_base 0, idle         ; I/O high: nothing (2 strobes a check)
    pau crc_reset [20]          ; a start bit: to the middle of data bit 0 (1.5 ETU)
    set x, 7
rbit:
    in pins, 1 [14]
    jmp x--, rbit
    in pins, 1                  ; parity, at 9.5 ETU
    pau crc_feed, 9             ; even parity: 0
    jmp pau crc_ok, good
    nop [4]
    set pins, 4 [23]            ; error signal: I/O low from 10.5 ETU, 1.5 ETU
    set pins, 5
    in null, 6
    in y, 1                     ; bit 15: the error flag
    push
    jmp idle
good:
    in null, 7                  ; data in bits 7:0, parity in bit 8
    push
    jmp idle
send:
    pull
    mov x, osr                  ; kept for a repetition
again:
    mov isr, osr
    pau crc_reset
    pau crc_feed, 16            ; the parity of the byte
    set pins, 4 [15]            ; start bit
tbit:
    out pins, 1 [14]
    jmp !status osr_empty, tbit
    jmp pau crc_ok, even
    set pins, 5 [14]            ; parity 1
    jmp check
even:
    set pins, 4 [15]            ; parity 0
check:
    set pins, 5 [15]            ; release at 10 ETU
    jmp in_base 0, idle         ; I/O high at 11 ETU: no error signal
    wait 1 in_base 0            ; the card's error signal ends
    mov osr, x [31]             ; the character again, 2 ETU later
    jmp again
