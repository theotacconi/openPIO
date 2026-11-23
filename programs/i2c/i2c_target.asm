.program i2c_target
; I2C target (peripheral) at a 7-bit address. SCL = in_base = sideset_base, SDA = SCL + 1 =
; set_base = out_base. Both pins in the chip open-drain mask (GPIO) with pull-ups; their latches
; stay 0, so a direction of 1 pulls a line low and 0 releases it.
; Y = 0x100 | address << 1 (address 0x50 here; the host writes Y to answer at another).
; RX FIFO, one word a byte: the address byte of a START (or repeated START) to this target, bit 8
; set (bits 7:1 the address, bit 0 R/W), then each byte written to it, bit 8 clear.
; TX FIFO: the bytes a controller reads (bits 7:0), loaded before the read or once its address
; byte is in the RX FIFO. SCL is held low (clock stretching) while the TX FIFO is empty on a read
; and while the RX FIFO is full on a write. When the controller NACKs a byte, the read is over
; and the bytes left in the TX FIFO are dropped.
; START and STOP are found anywhere, also inside a byte: the PAU's line state of SDA and SCL
; changes once, so one WAIT sees either line move. Not supported: general call, 10-bit addresses.
; SDA (an ACK, or a bit sent) is valid at most 9 strobes and 2 clocks after SCL falls, and set
; up 4 strobes or more before the target releases a stretched SCL (an ACK holds SCL from the
; strobe it pulls SDA: a late one stretches): UM10204's tVD;DAT and tSU;DAT at 45 MHz (the
; clock signed off) and at 50 MHz in Fast-mode Plus (1 MHz) at CLKDIV 1 to 2, Fast-mode
; (400 kHz) 2 to 4, Standard-mode (100 kHz) 4 to 18 (a fraction: its longer strobes count
; for tVD;DAT, its shorter ones for tSU;DAT).
.in_pins 2               ; SCL
.set_pins 3 1            ; SDA
.out_pins 3
.sideset_pins 2
.side_set 1 opt pindirs
.open_drain 2, 3
.pin_mask 2, 3
.shift_in left
.shift_out left
.y 0x1A0

    mov pins, null              ; latches 0: a direction of 1 pulls the line low
flush:                          ; drop what the TX FIFO holds
    set x, 0
    not x                       ; X = 0xFFFF: what PULL takes from an empty TX FIFO
    pull noblock
    mov x, osr
    not x
    jmp x--, flush
idle:
    mov pindirs, null           ; release SDA and SCL
hunt:
    wait 1 pau line_11          ; SDA and SCL high
    wait 0 pau line_11          ; one of them falls
    jmp !in_base 0, hunt        ; SCL: a bit for another target
start:                          ; SDA fell while SCL was high: a START
    set x, 1
    mov isr, x                  ; bit 8 = 1 after 8 bits: an address byte
    wait 0 in_base 0
byte:
    set x, 7
bit:
    wait 1 in_base 0            ; SCL rises: SDA is the bit
    jmp pau line_11, one
    in null, 1
    wait 0 pau line_01          ; until SCL falls, or SDA rises: a STOP
    jmp !in_base 0, next
    jmp idle
one:
    in pins, 1                  ; a 1 (SCL, high)
    wait 0 pau line_11          ; until SCL falls, or SDA falls: a repeated START
    jmp !in_base 0, next
    jmp start
next:
    jmp x--, bit
    mov x, isr
    jmp !x 8, ack               ; a byte written
    xor x, y                    ; an address byte: 0 ours to write, 1 ours to read
    jmp x--, not_write
ack:
    set pindirs, 1 side 1       ; ACK: SDA low, SCL held low until
    push block [2]              ; the RX FIFO takes the byte
    nop side 0
    wait 1 in_base 0            ; the controller clocks the ACK
    wait 0 in_base 0
    set pindirs, 0              ; release SDA
    jmp byte
not_write:
    jmp x--, idle               ; another target's address
    set pindirs, 1 side 1       ; ACK the read, SCL held low until
    push block [2]              ; the RX FIFO takes the address byte
    nop side 0
    wait 1 in_base 0
    wait 0 in_base 0
send:
    pull block side 1           ; SCL held low until the host gives the byte
    not osr                     ; direction = NOT bit: a 1 releases SDA
    out null, 8
    set x, 7
    out pindirs, 1 [3]          ; bit 7, set up 4 strobes before SCL is released
    nop side 0
sbit:
    wait 1 in_base 0
    wait 0 in_base 0
    jmp x--, sout
    set pindirs, 0              ; release SDA: the controller's ACK
    wait 1 in_base 0
    jmp pau line_11, flush      ; NACK: the read is over
    wait 0 in_base 0
    jmp send
sout:
    out pindirs, 1
    jmp sbit
