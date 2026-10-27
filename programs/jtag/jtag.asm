.program jtag
; JTAG (IEEE 1149.1) cycle engine: the host's software walks the TAP; every TCK cycle takes TMS
; and TDI from the TX FIFO and returns TDO. 6 strobes a cycle.
; Pins: TDI = out_base, TMS = out_base + 1, TCK = sideset_base, TDO = in_base.
; PIN_MASK: TDI, TMS, TCK. ISR and OSR shift right.
; TX FIFO: 8 cycles a word, cycle k from bits 2k (TDI) and 2k + 1 (TMS).
; RX FIFO: their 8 TDO bits in bits 15:8 (cycle 0 in bit 8).
; TMS / TDI change with TCK low; TDO, changed by the target on the falling edge, is sampled at
; the end of the high phase: TCK to the pin, the target's TDO and the input synchronizer take
; 4 clocks, so a shorter cycle would read the previous bit.
.out_pins 0              ; TDI, TMS
.sideset_pins 2          ; TCK
.in_pins 3               ; TDO
.pin_mask 0, 1, 2
.side_set 1
.push_thresh 8

    set y, 0 side 0
    not y side 0
    mov pindirs, y side 0       ; every pin of PIN_MASK an output
.wrap_target
    out pins, 2 auto side 0 [2] ; TCK low: TDI, TMS
    nop side 1 [1]              ; rising edge: the target samples TMS, TDI
    in pins, 1 auto side 1      ; TDO, as late as possible
.wrap
