.program spi_mode3
; SPI Master Mode 3 (CPOL=1, CPHA=1)
; SCK idle high. Data driven on SCK falling edge, sampled on SCK rising edge.
; Pin 0: SCK (side-set 1)
; Pin 1: MOSI (out_base 1)
; Pin 2: MISO (in_base 2)

.side_set 1
.wrap_target
    pull block side 1       ; Block until TX data available in TX FIFO; SCK idle high
    set x, 7 side 1         ; Transfer 8 bits per word
bit_loop:
    out pins, 1 side 0 [1]  ; Drive MOSI on falling edge (SCK=0, hold 2 cycles)
    in pins, 1 side 1 [1]   ; Sample MISO on rising edge (SCK=1, hold 2 cycles)
    jmp x--, bit_loop side 1 ; Loop back, keep SCK high
    push block side 1       ; Push received byte into RX FIFO
.wrap
