.program spi_target
; SPI target (peripheral), mode 0, MSB first, words of 1 to 16 bits (the push threshold, 8 here).
; Pins: SCK = 0, MOSI = in_base, CS_N = 2, MISO = out_base, driven only while CS_N is low.
; SCK half periods of at least 14 clocks at CLKDIV 1 (SCK up to clk / 28).
; TX FIFO: the answers, MSB in bit 15; X (0xFFFF, an idle-high MISO) when it is empty. As in a
; hardware target's shift register, a word's answer is taken when the word before it ends and
; waits there, its MSB on MISO, if the transfer ends first.
; RX FIFO: the words received, right-aligned. A word cut by CS_N rising is dropped (its answer is
; used up); one that finds the RX FIFO full is dropped with IRQ 0.
.in_pins 1
.out_pins 3
.set_pins 3 1
.shift_in left
.shift_out left
.push_thresh 8
.x 0xFFFF

idle:
    set pindirs, 0          ; MISO released
    mov isr, null           ; a word cut by CS_N: dropped
    wait 0 gpio 2           ; selected
    set pindirs, 1
word:
    pull noblock            ; the answer, or X
    out pins, 1             ; its MSB, before SCK rises
first:
    jmp gpio 2, held        ; CS_N high before the word started: keep it
    jmp !gpio 0, first
    in pins, 1              ; MOSI as SCK rises
    jmp low
bit:
    out pins, 1
high:
    jmp gpio 2, idle        ; CS_N high inside the word
    jmp !gpio 0, high
    in pins, 1
low:
    wait 0 gpio 0           ; mode 0: SCK falls before CS_N rises
    jmp !status isr_full, bit
    jmp status rx_full, full
    push
    jmp word
full:
    irq 0
    mov isr, null
    jmp word
held:
    set pindirs, 0
    wait 0 gpio 2
    set pindirs, 1          ; the MSB is still on the latch
    jmp first
