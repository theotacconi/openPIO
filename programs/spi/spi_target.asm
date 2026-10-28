.program spi_target
; SPI target (peripheral), mode 0, MSB first, 8-bit words. Pins: SCK = 0, MOSI = in_base,
; CS_N = 2, MISO = out_base, driven only while CS_N is low. SCK half periods of at least 14
; clocks at CLKDIV 1 (SCK up to clk / 28).
; TX FIFO: the answers, in bits 15:8; X (0xFFFF, an idle-high MISO) when it is empty. As in a
; hardware target's shift register, a word's answer is taken when the word before it ends and
; waits there, its MSB on MISO, if the transfer ends first.
; RX FIFO: the words received, in bits 7:0. A word cut by CS_N rising is dropped (its answer is
; used up); one that finds the RX FIFO full is dropped with IRQ 0.
.in_pins 1
.out_pins 3
.set_pins 3 1
.shift_in left
.shift_out left
.x 0xFFFF

idle:
    set pindirs, 0          ; MISO released
    wait 0 gpio 2           ; selected
    set pindirs, 1
word:
    pull noblock            ; the answer, or X
    set y, 7                ; 1 + 7 more bits
    out pins, 1             ; its MSB, before SCK rises
first:
    jmp gpio 2, held        ; CS_N high before the word started: keep it
    jmp !gpio 0, first
    in pins, 1              ; MOSI as SCK rises
    jmp low
bit:
    out pins, 1
high:
    jmp gpio 2, idle        ; CS_N high inside the word: drop it
    jmp !gpio 0, high
    in pins, 1
low:
    jmp gpio 2, idle
    jmp gpio 0, low
    jmp y--, bit
    jmp status rx_full, full
    push noblock
    jmp word
full:
    irq 0
    jmp word
held:
    set pindirs, 0
    wait 0 gpio 2
    set pindirs, 1          ; the MSB is still on the latch
    jmp first
