.program onewire
; 1-Wire Master Protocol Implementation (Dallas / Maxim standard)
; Pin 0: DQ (bidirectional open-drain via pindirs bit 0, external pull-up)
; jmp_pin: 0, set_base: 0, out_base: 0, in_base: 0

.wrap_target
    pull block              ; Block until command/data available in TX FIFO

    ; Master Reset Pulse: drive DQ low for 24 cycles
    set pindirs, 1 [23]
    ; Release line and wait 5 cycles for slave presence response
    set pindirs, 0 [4]
    ; Sample DQ: if line is high (no pull-down), presence pulse is absent
    jmp pin, no_presence

    ; Presence detected!
    irq 0                   ; Raise Presence Detected IRQ 0
    jmp slot_start

no_presence:
    irq 1                   ; Raise No-Presence Error IRQ 1
    jmp done

slot_start:
    ; Await presence pulse release by slave
    wait 1 gpio 0 [2]
    set x, 7                ; Transfer 8 bits

bit_loop:
    out y, 1                ; Extract bit from OSR
    jmp !y, write_zero

    ; Write-1 / Read Time Slot:
    ; Pull low briefly (2 cycles) to initiate slot, then release
    set pindirs, 1 [1]
    set pindirs, 0 [2]      ; Release and allow line to float high
    in pins, 1 [3]          ; Sample slave response (or master 1) at ~15us
    jmp bit_next

write_zero:
    ; Write-0 Time Slot:
    ; Hold low for full slot duration (7 cycles), then release 2 cycles for recovery
    set pindirs, 1 [6]
    set pindirs, 0 [1]

bit_next:
    jmp x--, bit_loop       ; Loop for remaining bits
    push noblock            ; Push captured 8-bit response into RX FIFO

done:
.wrap
