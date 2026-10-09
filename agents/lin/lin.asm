.program lin_master
; LIN (Local Interconnect Network) Master Header Generator
; Pin 0: TX (Single-wire dominant 0 / recessive 1 line)
; Timing: 4 machine cycles per nominal bit time
; Features:
;   - Sync Break generation (>= 13 bit times dominant low)
;   - Break Delimiter (>= 1 bit time recessive high)
;   - Sync Field serialization (0x55 with start and stop bits)
;   - Protected Identifier (PID) serialization from TX FIFO

.wrap_target
    pull block          ; Fetch PID word from TX FIFO

    ; 1. Sync Break: Dominant 0 for 13 bit periods (13 * 4 = 52 cycles)
    set x, 12           ; Loop 13 times (12 down to 0)
break_loop:
    set pins, 0 [2]     ; Drive low, delay 2 (1 + 2 = 3 cycles)
    jmp x--, break_loop ; Loop branch (1 cycle) -> 4 cycles per loop iteration

    ; 2. Break Delimiter: Recessive 1 for 1 bit period (4 cycles)
    set pins, 1 [3]

    ; 3. Sync Field (0x55 = 0b01010101):
    set pins, 0 [3]     ; Start bit (0)
    set x, 3            ; 4 pairs of 1 and 0 (8 bits total)
sync_loop:
    set pins, 1 [3]     ; High bit (4 cycles)
    set pins, 0 [3]     ; Low bit (4 cycles)
    jmp x--, sync_loop  ; Loop for remaining bits
    set pins, 1 [3]     ; Stop bit (1)

    ; 4. Protected Identifier (PID):
    set pins, 0 [3]     ; Start bit (0)
    set x, 7            ; 8 PID bits (LSB first)
pid_loop:
    out pins, 1 [2]     ; Shift PID bit onto TX (3 cycles)
    jmp x--, pid_loop   ; Loop branch (1 cycle) -> 4 cycles per bit
    set pins, 1 [3]     ; Stop bit (1)

    ; Header Complete:
    irq 0               ; Raise Header Generated IRQ 0
    push noblock        ; Notify host CPU
.wrap
