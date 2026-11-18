.program i2c_master
; I2C controller. SDA = set_base = out_base = in_base, SCL = SDA - 1 = sideset_base. Both pins in
; the chip open-drain mask (GPIO) with pull-ups: a 1 releases the line, a 0 pulls it low.
; PIN_MASK: SCL and SDA only (the program enables the direction of every pin in it).
; Host command, one per byte (TX FIFO, MSB first):
;   [15] START (or repeated START) first, [14] READ, [13:6] byte to write (ignored on reads),
;   [5] NACK: a read answers NACK instead of ACK, [4] STOP after the byte.
; Result, one per byte (RX FIFO, bits 8:0): the 8 bits seen on SDA, then the ACK bit (0 = ACK).
; Arbitration: a 1 written but read back as 0 means another controller drives the bus: the SM
; releases both lines, raises IRQ 0 and waits until the host clears it.
; SDA changes only while SCL is low; SCL high waits for the line to really rise (clock
; stretching). A bit is 20 strobes or more (22 at CLKDIV 1, the input synchronizer's delay): SCL
; up to strobes / 20. SCL is low 11 strobes, high 9 or more, SDA set 9 before SCL rises; a
; (repeated) START is set up and held 12, a STOP set up 10, the bus free 34 between a STOP and a
; START (measured; more at CLKDIV 1). At SCL = strobes / 20
; these meet UM10204's Table 10 at 100 kHz (Standard-mode), 400 kHz (Fast-mode) and 1 MHz
; (Fast-mode Plus), where SCL must be low longer than high and a target may change SDA up to
; 45 % of a bit after SCL falls. CLKDIV: an integer, or 2 or more with a fraction (below 2 a
; fraction makes strobes of 1 and 2 clocks, and a phase up to a clock shorter than its strobes).
.set_pins 3 1            ; SDA
.out_pins 3
.in_pins 3
.sideset_pins 2          ; SCL
.open_drain 2, 3
.pin_mask 2, 3
.shift_in left
.shift_out left
.side_set 1 opt

    set pins, 1 side 1          ; release SDA and SCL in the latch first,
    set y, 0
    not y
    mov pindirs, y              ; then enable them (PIN_MASK keeps the other pins)
    set dlymul, 2               ; delays x 2 from here: an instruction takes 1 + 2 x delay strobes
.wrap_target
cmd:
    pull block                  ; SCL as it was: high when idle, low between bytes
    out y, 1                    ; START?
    jmp !y, byte
    set pins, 1 [4]             ; release SDA (SCL may be low: repeated START)
    nop side 1                  ; release SCL
    wait 1 in_base 15 [5]       ; SCL (pin in_base - 1) really high; tSU;STA
    set pins, 0 [4]             ; START: SDA falls while SCL is high; tHD;STA
byte:
    out y, 1                    ; READ?
    set x, 7
    jmp !y, wbit

    out null, 8 side 0          ; READ: SCL low, no byte to write
    set pins, 1                 ; release SDA: the target drives it
rbit:
    nop side 0 [5]              ; strobes 0-10: SCL low
    nop side 1                  ; strobe 11: release SCL
    wait 1 in_base 15
    in pins, 1 [3]              ; sample SDA while SCL is high
    jmp x--, rbit
    out y, 1 side 0             ; SCL low; NACK?
    jmp !y, rack
    set pins, 1 [3]             ; NACK: leave SDA released
    jmp aclk [1]
rack:
    set pins, 0 [4]             ; ACK: pull SDA low
aclk:
    nop side 1
    wait 1 in_base 15
    in pins, 1 [3]              ; the ACK bit as seen on SDA
    jmp tail

wbit:
    out y, 1 side 0             ; strobe 0: SCL low; Y = next bit
    jmp !y, w0
    set pins, 1 [4]             ; 1: release SDA
    nop side 1                  ; strobe 11: release SCL
    wait 1 in_base 15
    in pins, 1 [2]
    jmp !in_base 0, lost        ; released but low: arbitration lost
    jmp x--, wbit
    jmp wack
w0:
    set pins, 0 [4]             ; 0: pull SDA low
    nop side 1
    wait 1 in_base 15
    in pins, 1 [2]
    nop                         ; as long as the arbitration check
    jmp x--, wbit
wack:
    out null, 1 side 0 [1]      ; SCL low; no NACK bit on writes
    set pins, 1 [4]             ; release SDA: the target ACKs
    nop side 1
    wait 1 in_base 15
    in pins, 1 [3]              ; the target's ACK bit

tail:
    out y, 1 side 0             ; SCL low; STOP?
    jmp !y, done
    set pins, 0 [4]             ; SDA low while SCL is low
    nop side 1
    wait 1 in_base 15 [4]       ; tSU;STO
    set pins, 1 [4]             ; STOP: SDA rises while SCL is high; tBUF
done:
    push block                  ; 8 bits seen on SDA + ACK bit
.wrap

lost:
    set pins, 1 side 1          ; release the bus
    mov isr, null
    irq wait 0                  ; until the host clears IRQ 0
    jmp cmd
