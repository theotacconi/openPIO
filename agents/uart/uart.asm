.program uart_rx
; UART Receiver with Hardware Framing Check
; Pin 0: RX (in_base=0, jmp_pin=0)
; Baud rate: 8 cycles per bit period
; Timing:
;   - Start bit detected via wait 0
;   - Delay 10 cycles + 1 cycle set x to land at cycle 12 (midpoint of bit 0)
;   - Sample 8 bits (in pins, 1 [6] + jmp x-- = 8 cycles per bit)
;   - Delay 7 cycles to reach midpoint of stop bit
;   - Sample stop bit: if 1 -> push noblock; if 0 -> irq 0 (framing error)

.wrap_target
wait_start:
    wait 0 gpio 0 [10]  ; Wait for falling start bit; delay to center on bit 0
    set x, 7            ; 8 data bits (X decrements from 7 down to 0)

bit_loop:
    in pins, 1 [6]      ; Sample bit into ISR, wait 6 cycles
    jmp x--, bit_loop   ; Loop decrement (1 cycle) -> 1 + 6 + 1 = 8 cycles per bit

    nop [7]             ; Delay to center of stop bit
    jmp pin, stop_ok    ; Sample stop bit: if high, frame is valid!

    irq 0               ; Framing error: stop bit missing/low! Raise IRQ 0
    jmp wait_idle       ; Discard malformed frame and await line idle

stop_ok:
    push noblock        ; Valid frame: push ISR to RX FIFO

wait_idle:
    wait 1 gpio 0       ; Await line return to idle high before next frame
.wrap
