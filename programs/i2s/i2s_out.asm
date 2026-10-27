.program i2s_out
; I2S master, 16-bit stereo, Philips format: BCLK, LRCLK (low = left) and SDOUT to a codec's
; DAC; its peer runs i2s_in in lockstep to read the codec's ADC. 2 strobes a BCLK period.
; TX FIFO: samples, left then right, one a word. It never stalls: an empty FIFO sends 0
; (silence) and BCLK keeps running.
.side_set 2              ; bit 0 BCLK, bit 1 LRCLK
.sideset_pins 1          ; BCLK = 1, LRCLK = 2
.out_pins 0              ; SDOUT
.set_pins 0 3
.pin_mask 0, 1, 2
.shift_out left
.pull_thresh 15          ; !osre: 15 bits out, the LSB comes after the loop

    set pindirs, 7   side 0b10   ; LRCLK high: an empty right word ends
    nop              side 0b11
    nop              side 0b00   ; its LSB; LRCLK low: left next
.wrap_target
    pull noblock     side 0b01   ; left sample (X = 0 when the FIFO is empty)
left:
    out pins, 1      side 0b00   ; data changes with BCLK low
    jmp !osre, left  side 0b01
    out pins, 1      side 0b10   ; LSB; LRCLK high one bit before the right MSB
    pull noblock     side 0b11
right:
    out pins, 1      side 0b10
    jmp !osre, right side 0b11
    out pins, 1      side 0b00   ; LSB; LRCLK low: left next
.wrap
