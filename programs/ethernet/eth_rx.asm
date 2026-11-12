.program eth_rx
; 10BASE-T receiver: RD+ = in_base (through a line receiver), Manchester with the PAU at 3 strobes
; a half-bit: 10 Mbit/s with the chip clocked at 60 MHz, CLKDIV 1. The program finds the SFD
; (two 1s in a row after the preamble's 1, 0, ...), the PAU decodes and checks the FCS (CRC-32:
; the residue of a right frame), the frame ends when the line has no edge for 2 bit times.
; RX FIFO, a frame: data words of 15 bits in [15:1], bit 0 = 0 (shift right: the oldest bit in
; bit 1); then an end word, bit 0 = 1: the bits left, then a 1 above them, zeros above it; then
; the verdict: 1 when the FCS is right, else 0. The bits are the frame from the destination
; address to the FCS, LSB first, and the bit or two the decoder takes from TP_IDL after it: the
; host keeps whole bytes and drops the FCS. FIFO_SPLIT 1: the RX FIFO holds 253 words (3795
; bits, a 470-byte frame); an SPI host at SCLK clock / 6 pops a word every 97.5 clocks, the line
; gives one every 90: a 1518-byte frame (812 words) leaves about 60 words behind, a frame up to
; about 6000 bytes fits.
.in_pins 0                      ; RD+
.shift_in right
.push_thresh 15
.fifo_split 1                   ; TX ring 8 words, RX ring 248
.pau code=manchester baud=3
.crc width=32 poly=0x04C11DB7 init=ones res=0xC704DD7B

frame:
    set pau_ctl, 0
    set pau_ctl, rx|crc         ; armed for the first edge
hunt:                           ; the SFD: a 1, then a 1
    mov isr, null
    in pins, 1
    mov x, isr
    jmp x--, one
    jmp hunt
one:
    mov isr, null
    in pins, 1
    mov x, isr
    jmp x--, sfd
    jmp hunt
sfd:
    pau crc_reset               ; before the first frame bit is decoded
    mov isr, null
    mov y, null                 ; bits since the CRC was last right, - 1
take:
    in pins, 1 auto
    jmp !pau crc_ok, older
    set y, 2
older:
    jmp y--, bit
bit:
    jmp pau rx_ready, take
    jmp !pau rx_idle, bit
    set x, 1                    ; the end: a 1 above the bits left
    in x, 1
fill:
    jmp status isr_full, done
    in null, 1
    jmp fill
done:
    or isr, x                   ; bit 0: the end word
    push
    set x, 0
    jmp y--, last               ; right when the CRC was right after the last bit taken
    jmp right                   ; or the one before (a bit from TP_IDL)
last:
    jmp y--, wrong
right:
    set x, 1
wrong:
    mov isr, x
    push
    jmp frame
