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
; stretching). A bit is about 11 strobes: SCL = strobes / 11.
.side_set 1 opt

    set pins, 1 side 1          ; release SDA and SCL in the latch first,
    set y, 0
    not y
    mov pindirs, y              ; then enable them (PIN_MASK keeps the other pins)
.wrap_target
cmd:
    pull block                  ; SCL as it was: high when idle, low between bytes
    out y, 1                    ; START?
    jmp !y, byte
    set pins, 1 [3]             ; release SDA (SCL may be low: repeated START)
    nop side 1                  ; release SCL
    wait 1 in_base 7            ; SCL (pin in_base - 1) really high
    set pins, 0 [3]             ; START: SDA falls while SCL is high
byte:
    out y, 1                    ; READ?
    set x, 7
    jmp !y, wbit

    out null, 8 side 0          ; READ: SCL low, no byte to write
    set pins, 1                 ; release SDA: the target drives it
rbit:
    nop side 0 [3]
    nop side 1                  ; release SCL
    wait 1 in_base 7
    in pins, 1 [1]              ; sample SDA while SCL is high
    jmp x--, rbit
    out y, 1 side 0             ; SCL low; NACK?
    jmp !y, rack
    set pins, 1 [1]             ; NACK: leave SDA released
    jmp aclk
rack:
    set pins, 0 [1]             ; ACK: pull SDA low
aclk:
    nop side 1
    wait 1 in_base 7
    in pins, 1                  ; the ACK bit as seen on SDA
    jmp tail

wbit:
    out y, 1 side 0             ; SCL low; Y = next bit
    jmp !y, w0
    set pins, 1 [1]             ; 1: release SDA
    nop side 1
    wait 1 in_base 7
    in pins, 1
    jmp !in_base 0, lost        ; released but low: arbitration lost
    jmp x--, wbit
    jmp wack
w0:
    set pins, 0 [1]             ; 0: pull SDA low
    nop side 1
    wait 1 in_base 7
    in pins, 1
    nop                         ; as long as the arbitration check
    jmp x--, wbit
wack:
    out null, 1 side 0          ; SCL low; no NACK bit on writes
    set pins, 1 [1]             ; release SDA: the target ACKs
    nop side 1
    wait 1 in_base 7
    in pins, 1                  ; the target's ACK bit

tail:
    out y, 1 side 0 [1]         ; SCL low; STOP?
    jmp !y, done
    set pins, 0 [1]             ; SDA low while SCL is low
    nop side 1
    wait 1 in_base 7
    set pins, 1 [3]             ; STOP: SDA rises while SCL is high
done:
    push block                  ; 8 bits seen on SDA + ACK bit
.wrap

lost:
    set pins, 1 side 1          ; release the bus
    mov isr, null
    irq wait 0                  ; until the host clears IRQ 0
    jmp cmd
