.program nec_rx
; NEC infrared remote receiver, on the output of an IR receiver module (low during a 38 kHz
; burst, idle high). 1 strobe = 1 us (CLKDIV = clock in MHz).
; Leader: a burst of at least 7.7 ms (9 ms), then a space: 4.5 ms for a frame, 2.25 ms for a
; repeat code (key held). Bits: a 562 us burst, then a space of 562 us (0) or 1687 us (1),
; sampled 838 us into it. Shorter bursts outside a frame are ignored.
; RX FIFO, a frame: two words, as sent LSB first: address | ~address << 8, command | ~command
; << 8 (the host checks the complements). A repeat code raises IRQ 0.
.in_pins 0
.shift_in right

    set dlymul, 31              ; [n] = 31 n strobes
.wrap_target
idle:
    wait 1 in_base 0
    wait 0 in_base 0            ; a burst starts
    set x, 7
leader:
    jmp x--, leader [31]        ; 8 x 962 us: 7.7 ms
    jmp in_base 0, idle         ; already over: not a leader
    wait 1 in_base 0
    set x, 15
space:
    jmp !in_base 0, repeat      ; a burst within 3.5 ms: repeat code
    jmp x--, space [7]          ; 16 x 219 us
    set x, 1                    ; a frame: two words
word:
    set y, 15
bit:
    wait 0 in_base 0            ; burst
    wait 1 in_base 0            ; space
    nop [27]                    ; 838 us into it
    in pins, 1                  ; still high: 1
    jmp y--, bit
    push noblock
    jmp x--, word
    jmp idle
repeat:
    irq 0
.wrap
