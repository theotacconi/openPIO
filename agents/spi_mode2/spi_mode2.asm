.program spi_mode2
; SPI Master Mode 2 (CPOL=1, CPHA=0)
; SCK idle high. Data sampled on SCK falling edge, shifted on SCK rising edge.
; Pin 0: SCK (side-set 1)
; Pin 1: MOSI (out_base 1)
; Pin 2: MISO (in_base 2)

.side_set 1
.wrap_target
    pull block side 1       ; Block until TX data available in TX FIFO; SCK idle high
    set x, 7 side 1         ; Transfer 8 bits per word
bit_loop:
    out pins, 1 side 1 [1]  ; Drive MOSI while SCK is HIGH (hold 2 cycles)
    in pins, 1 side 0 [1]   ; Sample MISO on falling edge (SCK=0, hold 2 cycles)
    jmp x--, bit_loop side 1 ; Loop back, return SCK to HIGH
    push block side 1       ; Push received byte into RX FIFO
.wrap
