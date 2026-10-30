.program spi_pau_target
; SPI target (peripheral) through the PAU on SCK, its external bit clock: mode 0 (CPOL and CPHA
; in PAU_MODE give the others), MSB first, 8-bit words. The PAU samples MOSI and shifts MISO on
; the clock's edges; the program only moves bytes.
; Pins: MOSI = in_base, SCK = in_base + 1, CS_N = 2, MISO = out_base, driven while CS_N is low.
; TX FIFO: the answers, a byte in bits 15:8; 0xFF when it is empty. As in a hardware target's
; shift register, a byte's answer is taken when the byte before it ends and waits there, its MSB
; on MISO, if the transfer ends first. RX FIFO: the bytes received, in bits 7:0. A byte cut by
; CS_N rising is dropped, its answer used up.
.in_pins 0
.out_pins 3
.set_pins 3 1
.pau code=nrz ext
.shift_in left
.shift_out left

    xnor x, x               ; 0xFFFF: no answer queued (and the answer when the FIFO is empty)
    set y, 7                ; X = 7: an answer queued, no bit of its byte yet
cut:
    pau tx_abort            ; a cut answer: dropped
    set pau_ctl, 0
    mov isr, null           ; a cut byte: dropped
    xnor x, x
idle:
    set pindirs, 0          ; MISO released
    wait 0 gpio 2           ; selected
    set pindirs, 1
    set pau_ctl, tx|rx      ; clears the RX queue; an answer that waited stays queued
    jmp x!=y, byte          ; none waited: take one
    jmp bit
byte:
    pull noblock            ; the answer, or X
    out pins, 8             ; queued: its MSB on MISO at once (CPHA 0)
    set x, 7
bit:
    jmp gpio 2, held        ; CS_N high
    jmp !pau rx_ready, bit
    in pins, 1              ; a bit the PAU sampled as SCK rose
    jmp x--, bit
    push noblock            ; X is 0xFFFF again
    jmp byte
held:
    jmp x!=y, cut           ; inside a byte
    jmp idle                ; before it: its answer waits
