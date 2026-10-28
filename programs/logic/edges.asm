.program edges
; Edge timestamps of pin in_base: the time (IN TIME, strobes since RESET, 16 bits, wrapping) of
; every rising edge, then of the falling edge after it, and so on: input capture for periods,
; pulse widths and unknown protocols (an IR remote, a sensor's PWM). Times are the strobe the
; edge is seen in plus 1, the same for every edge; edges at least 2 strobes apart.
; RX FIFO: the times, alternately of rising and falling edges (from a low line).
.in_pins 0

    wait 0 in_base 0        ; a low line first
.wrap_target
    wait 1 in_base 0
    in time, 16 auto
    wait 0 in_base 0
    in time, 16 auto
.wrap
