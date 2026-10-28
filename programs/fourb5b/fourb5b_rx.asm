.program fourb5b_rx
; Biphase mark (BMC) decoded by the PAU, 4b5b symbols framed in software: the USB Power Delivery
; receive path. Pin: in_base. 8 strobes a bit (PAU half-bit 4), as fourb5b_tx.
; A frame is a preamble of alternating bits ending in 1, then 5-bit symbols LSB first: the first
; two 0s in a row start the first symbol (Sync-1, 11000: sent 0, 0, 0, 1, 1).
; RX FIFO: one symbol a word, in bits 15:11 (the host decodes the K-codes and 4b5b), until the
; line is idle for 2 bit times. A frame the RX FIFO could not take (PAU overrun) raises IRQ 0.
.in_pins 0
.pau enc=bmc baud=4
.push_thresh 5

    jmp start
overrun:
    irq 0
.wrap_target
start:
    set pau_ctl, 0
    set pau_ctl, rx         ; armed: the first edge is a bit boundary
    xnor isr, isr           ; ISR all 1s: no 0s seen yet
hunt:
    in pins, 1              ; ISR shifts right: the newest bit is bit 15
    mov x, isr
    jmp x 15, hunt
    jmp x 14, hunt
    out isr, 2              ; two 0s: ISR = 2 bits, the symbol's first two
sym:
    jmp pau rx_ready, take
    jmp !pau rx_idle, sym
    mov x, status           ; the frame is over: PAU status 7, overrun, is bit 15
    jmp x 15, overrun
.wrap
take:
    in pins, 1 auto         ; a symbol every 5 bits
    jmp sym
