.program can_controller
; CAN Bit-Level Controller with Real-Time Arbitration Monitoring
; Pin 0: TX (drive Dominant 0 or Recessive 1)
; Pin 1: RX (bus monitor / transceiver loopback, mapped to jmp_pin = 1)
; set_base: 0, out_base: 0, in_base: 0, jmp_pin: 1

.wrap_target
    pull block              ; Wait for 11-bit identifier / message header
    set pins, 1             ; Idle recessive on bus
    set x, 10               ; 11 identifier bits (10 down to 0)

bit_loop:
    out y, 1                ; Shift current bit into Y
    jmp !y, send_dominant   ; If 0, drive dominant

    ; Transmitting Recessive (1):
    set pins, 1 [2]         ; Drive TX high (recessive), hold 3 cycles
    jmp pin, arb_ok         ; Sample RX via jmp_pin: if 1, bus is recessive -> WON!

    ; Collision / Arbitration Lost:
    irq 1                   ; Bus is Dominant while TX is Recessive -> Raise IRQ 1
    set pins, 1             ; Instantly cease driving and stay recessive
    jmp arb_exit            ; Abort frame and wait for bus idle

arb_ok:
    jmp bit_end

send_dominant:
    ; Transmitting Dominant (0):
    set pins, 0 [2]         ; Drive TX low (dominant), hold 3 cycles

bit_end:
    jmp x--, bit_loop       ; Loop for remaining identifier bits

    ; Entire Identifier Won:
    irq 0                   ; Raise Arbitration Won IRQ 0
    push noblock            ; Signal host CPU that arbitration is won
    jmp done

arb_exit:
    wait 1 gpio 1           ; Stalls until competing transmission finishes and line returns idle

done:
.wrap
