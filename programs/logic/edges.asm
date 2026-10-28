.program edges
; Edge timestamps of pin in_base: first the pins as they are (bit 0: its level), then the time
; (IN TIME, strobes since RESET, 16 bits, wrapping) of every edge, alternately rising and
; falling from that level: input capture for periods, pulse widths and unknown protocols (an IR
; remote, a sensor's PWM, a recording for sim/learn). Times are the strobe the edge is seen in
; plus 1, the same for every edge; edges at least 2 strobes apart.
.in_pins 0

    in pins, 16 auto        ; the level at the start
    jmp in_base 0, high
.wrap_target
    wait 1 in_base 0
    in time, 16 auto
high:
    wait 0 in_base 0
    in time, 16 auto
.wrap
