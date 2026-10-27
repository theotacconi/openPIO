.program i2s_in
; I2S input from a codec's ADC, in lockstep with i2s_out on the peer SM: same CLKDIV, started
; by the same CTRL write. It samples SDIN at the end of each BCLK period (the falling strobe of
; i2s_out), when the bit is settled: CLKDIV >= 2 (the codec answers a falling edge, the input
; synchronizer adds 2 clocks). RX FIFO: samples, left then right; dropped when full, never
; stalls.
.in_pins 3               ; SDIN
.shift_in left

    nop [5]                     ; strobes 0..5: i2s_out's start, until the end of the left MSB
.wrap_target
    in pins, 1
    push iffull noblock         ; every 16 bits
.wrap
