.program jtag_tap
; IEEE 1149.1 Standard Test Access Port (JTAG) Controller
; Pin 0: TCK (Test Clock, driven via side-set base 0)
; Pin 1: TMS (Test Mode Select, bit 1 via set pins)
; Pin 2: TDI (Test Data In, bit 2 via out pins)
; Pin 3: TDO (Test Data Out, in_base 3)

.side_set 1
.wrap_target
    pull block side 0       ; Fetch 8-bit TDI data word from TX FIFO

    ; 1. Reset TAP: 5 rising clock edges with TMS=1
    set x, 4 side 0
tap_reset:
    set pins, 2 side 0 [1]  ; TMS=1, TCK=0 (hold 2 cycles)
    nop side 1 [1]          ; TCK=1 (rising edge, hold 2 cycles)
    jmp x--, tap_reset side 0

    ; 2. Navigate to Shift-DR:
    ; Reset -> Run-Test/Idle: TMS=0
    set pins, 0 side 0 [1]
    nop side 1 [1]
    ; Run-Test/Idle -> Select-DR-Scan: TMS=1
    set pins, 2 side 0 [1]
    nop side 1 [1]
    ; Select-DR -> Capture-DR: TMS=0
    set pins, 0 side 0 [1]
    nop side 1 [1]
    ; Capture-DR -> Shift-DR: TMS=0
    set pins, 0 side 0 [1]
    nop side 1 [1]

    ; 3. Shift Data Phase (bits 0 to 6 in Shift-DR, TMS=0):
    set x, 6 side 0
shift_loop:
    out pins, 1 side 0 [1]  ; Drive TDI bit while holding TMS=0, TCK=0
    in pins, 1 side 1 [1]   ; Sample TDO on rising TCK
    jmp x--, shift_loop side 0

    ; 4. Shift Final Bit (bit 7) and transition to Exit1-DR: TMS=1
    set pins, 2 side 0 [1]  ; TMS=1
    in pins, 1 side 1 [1]   ; Sample bit 7 TDO on rising TCK

    ; 5. Navigate to Run-Test/Idle:
    ; Exit1-DR -> Update-DR: TMS=1
    set pins, 2 side 0 [1]
    nop side 1 [1]
    ; Update-DR -> Run-Test/Idle: TMS=0
    set pins, 0 side 0 [1]
    nop side 1 [1]

    irq 0 side 0            ; TAP transaction complete IRQ 0
    push noblock side 0
.wrap
