.program record
; A recording of all 16 pins: first the pins as they are, then at every change the time (IN
; TIME, strobes since RESET, 16 bits, wrapping) and the pins after it. A change is seen in the
; strobe after it reaches the SM; the pins are read 2 strobes later, so changes at least 3
; strobes apart are each a record of their own, closer ones share one. For sim/learn: protocols
; with a clock and data, and anything with more than one wire.
; RX FIFO: pins, then (time, pins) pairs.

    in pins, 16 auto
.wrap_target
    wait 1 status pins_changed
    in time, 16 auto
    in pins, 16 auto
.wrap
