.program spi_mode1
; SPI Master Mode 1 (CPOL=0, CPHA=1)
; SCK idle low. Data driven on SCK rising edge, sampled on SCK falling edge.
; Pin 0: SCK (side-set 1)
; Pin 1: MOSI (out_base 1)
; Pin 2: MISO (in_base 2)

.side_set 1
.wrap_target
    pull block side 0       ; Block until TX data available in TX FIFO; SCK idle low
    set x, 7 side 0         ; Transfer 8 bits per word
bit_loop:
    out pins, 1 side 1 [1]  ; Drive MOSI on rising edge (SCK=1, hold 2 cycles)
    in pins, 1 side 0 [1]   ; Sample MISO on falling edge (SCK=0, hold 2 cycles)
    jmp x--, bit_loop side 0 ; Loop back, keep SCK low
    push block side 0       ; Push received byte into RX FIFO
.wrap
