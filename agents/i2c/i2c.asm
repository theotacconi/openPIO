.program i2c_master
; I2C Master with Hardware Clock Stretching and Multi-Master Arbitration
; Pin 0: SCL (open drain via pindirs bit 0, external pull-up)
; Pin 1: SDA (open drain via pindirs bit 1, external pull-up)
; jmp_pin: 1 (SDA)
; in_base: 0, set_base: 0, out_base: 0

.wrap_target
    pull block              ; Block until TX word available

    ; Generate START Condition: SDA falls while SCL high
    set pindirs, 2 [1]      ; Pull SDA low (pindir bit 1 = 1)
    set pindirs, 3 [1]      ; Pull SCL low (pindir bits 0 and 1 = 1)
    set x, 7                ; 8 bits to transmit

bit_loop:
    out y, 1                ; Shift MSB into Y
    jmp !y, drive_zero      ; If 0, drive SDA low; if 1, release SDA high

    ; Transmit 1: Release SDA high (pindirs = 1: SCL driven low, SDA released)
    set pindirs, 1
    set pindirs, 0          ; Release SCL high
    wait 1 gpio 0           ; Clock Stretching: wait until SCL actually rises high
    jmp pin, arb_ok         ; Sample SDA: if high, arbitration won; if low, COLLISION!

    ; Multi-Master Arbitration Lost:
    irq 1                   ; Raise Arbitration Loss IRQ 1
    jmp abort               ; Abort transfer immediately and release bus

arb_ok:
    jmp bit_end

drive_zero:
    set pindirs, 3          ; SCL low, SDA low
    set pindirs, 2          ; Release SCL high while holding SDA low
    wait 1 gpio 0           ; Clock Stretching: wait until SCL rises high

bit_end:
    set pindirs, 3          ; Pull SCL low to complete bit period
    jmp x--, bit_loop       ; Loop for remaining bits

    ; ACK Phase: Release SDA, release SCL, sample ACK
    set pindirs, 1          ; Release SDA
    set pindirs, 0          ; Release SCL
    wait 1 gpio 0           ; Clock Stretching on ACK
    in pins, 2              ; Sample ACK bit from SDA into ISR
    set pindirs, 3          ; Pull SCL low

    ; Generate STOP Condition: SCL rises, then SDA rises
    set pindirs, 2          ; SDA low, SCL released
    wait 1 gpio 0           ; Wait for SCL high
    set pindirs, 0 [1]      ; Release SDA high (STOP condition)
    push noblock            ; Push ACK result to RX FIFO
    jmp done

abort:
    set pindirs, 0          ; Collision abort: completely release bus (SCL=float, SDA=float)

done:
.wrap
