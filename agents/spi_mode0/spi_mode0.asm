.program spi_mode0
; SPI Master Mode 0 (CPOL=0, CPHA=0)
; SCK idle low. Data sampled on SCK rising edge, shifted on SCK falling edge.
; Pin 0: SCK (side-set 1)
; Pin 1: MOSI (out_base 1)
; Pin 2: MISO (in_base 2)

.side_set 1
.wrap_target
    pull block side 0       ; Block until TX data available in TX FIFO; SCK idle low
    set x, 7 side 0         ; Transfer 8 bits per word
bit_loop:
    out pins, 1 side 0 [1]  ; Drive MOSI bit while SCK is LOW (hold 2 cycles)
    in pins, 1 side 1 [1]   ; Sample MISO bit while SCK is HIGH (rising edge, hold 2 cycles)
    jmp x--, bit_loop side 0 ; Decrement bit count, return SCK to LOW
    push block side 0       ; Push received word from ISR into RX FIFO; SCK remains low
.wrap
