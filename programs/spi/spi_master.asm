.program spi_master
; SPI master, any of the 4 modes, 8 bits a word, MSB first, CS_N low around each word.
; X = mode (bit 1 CPOL, bit 0 CPHA), set by the host; Y = 1 << SCK pin.
; Pins: MOSI = out_base = set_base, SCK = set_base + 1, CS_N = set_base + 2 = sideset_base
;   (set_count 3), MISO = in_base. PIN_MASK: MOSI, SCK, CS_N.
; TX FIFO: byte in bits 15:8; RX FIFO: byte in bits 7:0. 6 strobes a bit: SCK = strobes / 6.
; SCK is toggled with XOR (xor pins, y), so CPOL is only the level it starts from.
.out_pins 0              ; MOSI
.set_pins 0 3            ; MOSI, SCK, CS_N
.sideset_pins 2          ; CS_N
.in_pins 3               ; MISO
.pin_mask 0, 1, 2
.shift_in left
.shift_out left
.y 2                     ; 1 << SCK
.side_set 1 opt
.pull_thresh 8
.push_thresh 8

    set pins, 4             ; CS_N high, SCK and MOSI low
    set pindirs, 7
    jmp !x 1, word          ; CPOL 0: SCK idles low
    xor pins, y             ; CPOL 1: SCK idles high
word:
    pull block side 1       ; CS_N high between words
    jmp x 0, cpha1 side 0   ; CS_N low, one strobe before the first edge
cpha0:                      ; data out before the leading edge, sampled on it
    out pins, 1 [1]
    xor pins, y             ; leading edge
    in pins, 1 auto         ; MISO, pushed after 8 bits
    xor pins, y             ; trailing edge: the slave shifts
    jmp !osre, cpha0
    jmp word
cpha1:                      ; data changed after the leading edge, sampled on the trailing one
    xor pins, y             ; leading edge: the slave shifts MISO
    out pins, 1 [1]         ; MOSI, 2 strobes before the slave samples it
    xor pins, y             ; trailing edge
    in pins, 1 auto         ; MISO, 4 strobes after it changed (input synchronizer)
    jmp !osre, cpha1
    jmp word
