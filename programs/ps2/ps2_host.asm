.program ps2_host
; PS/2 host (keyboard, mouse). DATA = in_base = out_base = set_base, CLK = DATA + 1 (set_count 2),
; both in the chip open-drain mask with pull-ups; PIN_MASK: DATA, CLK. 1 strobe = 1 us.
; OSR and ISR shift right. The device clocks every bit; the host reads DATA on CLK falling edges
; and, sending, changes DATA while CLK is low.
; RX FIFO, a received frame: bits 15 stop, 14 parity, 13:6 data, 5 start; bit 0 set on a parity
;   error (odd parity, checked with CRC feed: a 1-bit CRC).
; TX FIFO: a byte to send (bits 7:0). RX FIFO then gets bit 15 = the device's ACK (0 = ACK).
.set_pins 2 2            ; DATA, CLK
.out_pins 2
.in_pins 2
.open_drain 2, 3
.pin_mask 2, 3
.crc width=1 poly=1

    set pins, 3             ; release CLK and DATA
    set pindirs, 3
idle:
    jmp !status tx_empty, send
    jmp in_base 1, idle     ; CLK high: nothing
    in pins, 1              ; CLK low: the device starts a frame (start bit)
    pau crc_reset
    set x, 8
rbit:
    wait fall in_base 1     ; data bits and parity
    in pins, 1
    jmp x--, rbit
    pau crc_feed, 9         ; odd parity: the XOR of data and parity is 1
    wait fall in_base 1
    in pins, 1              ; stop
    mov x, isr
    jmp !pau crc_ok, good
    set y, 1
    or x, y                 ; parity error
good:
    mov isr, x
    push
    wait 1 in_base 1
    jmp idle
send:
    pull block
    mov isr, osr
    pau crc_reset
    pau crc_feed, 16        ; the byte's parity
    set pins, 1             ; inhibit: CLK low, DATA released
    set dlymul, 4
    nop [25]                ; 101 us
    set dlymul, 1
    set pins, 0             ; DATA low (start bit)
    set pins, 2             ; release CLK: request to send
    set x, 7
tbit:
    wait fall in_base 1     ; the device clocks: next bit while CLK is low
    out pins, 1
    jmp x--, tbit
    wait fall in_base 1
    jmp !pau crc_ok, par0
    set pins, 3             ; parity 1 (even number of ones)
    jmp stop
par0:
    set pins, 2             ; parity 0
stop:
    wait fall in_base 1
    set pins, 3             ; stop bit: release DATA
    wait fall in_base 1     ; ACK pulse
    mov isr, null
    in pins, 1              ; DATA: 0 = ACK
    push
    wait 1 in_base 1
    jmp idle
