.program ws2812
; WS2812B LED chain (NeoPixel): one 1.25 us bit every 10 strobes, 1 strobe = 125 ns
; (CLKDIV = clock in MHz / 8, fractional allowed: 48 MHz -> 6, 50 MHz -> 6.25).
; A 0 is high 3 strobes then low 7, a 1 high 7 then low 3 (datasheet: 400 / 850, 800 / 450 ns,
; +-150). TX FIFO: the colours as a bit stream, G R B per LED, MSB first, 16 bits a word (pad
; the last word). With the FIFO empty the line stays low: after 50 us the LEDs latch.
.side_set 1
.sideset_pins 0          ; DIN
.set_pins 0 1
.shift_out left

    set pindirs, 1 side 0
.wrap_target
bit:
    out x, 1 auto side 0 [2]    ; low 3 (end of the bit before); waits low for data
    jmp !x, zero side 1 [2]     ; high 3
    jmp bit side 1 [3]          ; a 1: high 4 more
zero:
    nop side 0 [3]              ; a 0: low 4 more
.wrap
