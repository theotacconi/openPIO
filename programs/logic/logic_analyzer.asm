.program logic_analyzer
; Logic analyzer with a trigger the host picks at run time: any condition of ISA.md (a pin, an
; in_base pin, an IRQ flag, a bit of X or Y, an SM or PAU status; inverted with bit 7), taken
; when it becomes true; then n samples of the 16 pins, one every 2 strobes.
; TX FIFO: the trigger condition (byte, in slot 0), then n - 1. RX FIFO: the samples, bit i = pin i.
; Up to 133 samples (the RX FIFO) at any rate; more when the host reads them as they come.
.in_pins 0

.wrap_target
    pull block
    mov cond, osr           ; COND slot 0 = the trigger (OSR bits 9:8 = 0)
    pull block
    mov x, osr              ; samples - 1
    wait rise cond0         ; the trigger becomes true
sample:
    in pins, 16 auto        ; pins 0..15
    jmp x--, sample
.wrap
