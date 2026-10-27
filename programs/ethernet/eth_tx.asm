.program eth_tx
; 10BASE-T transmitter: TD+ = out_base = set_base, TD- = TD+ + 1 (set_count 2), Manchester with
; the PAU (2 strobes a half-bit: 10 Mbit/s with the chip clocked at 40 MHz, CLKDIV 1). The program
; sends preamble and SFD, the PAU the frame bits and the FCS (CRC-32); TP_IDL ends the frame.
; Host configuration (SM stopped): Y = 0x5555, the preamble pattern.
; TX FIFO: N = number of 16-bit words after the SFD, then those words (destination address to
; payload, LSB first, even byte count). The host sets flag 0 once a frame is in the FIFO (it has
; 132 words: frames up to 260 bytes), the program clears it, sends the frame, sets flag 1.
.pau enc=manchester diff baud=2
.crc width=32 poly=0x04C11DB7 init=ones xorout=1

    set pindirs, 3
.wrap_target
    wait 1 irq 0            ; the host has loaded a frame
    irq clear 0
    pull block
    mov x, osr              ; N
    set pau_ctl, tx
    mov osr, y              ; preamble and SFD: 63 bits 1, 0, 1, ... then a 1
    out pins, 16
    mov osr, y
    out pins, 16
    mov osr, y
    out pins, 16
    mov osr, y
    out pins, 15
    mov osr, y
    out pins, 1
    pau crc_reset
    set pau_ctl, tx|crc
    jmp next
word:
    pull block
    out pins, 16
next:
    jmp x--, word
    pau crc_append          ; FCS (appended bits never feed the CRC)
    set pins, 1             ; TP_IDL: TD+ high, waiting under the PAU
    set pau_ctl, 0          ; drain
    wait 1 pau tx_idle [7]  ; TP_IDL for 2 bit times
    set pins, 0             ; idle
    irq 1                   ; frame sent
.wrap
