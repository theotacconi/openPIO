.program usb_ls_tx
; USB Low-Speed (1.5 Mbps) Packet Transmitter
; Pin 0: D-, Pin 1: D+ (Low-Speed: J-state is D-=1, D+=0; K-state is D-=0, D+=1; SE0 is D-=0, D+=0)
; Baud timing: 4 machine cycles per nominal bit time
; PAU Features:
;   - set pau_cfg, 3 enables in-flight NRZI encoding (Bit 0) and Auto Bit-Stuffing (Bit 1)
;   - EOP: 2 bit times of SE0 followed by 1 bit time of J-state

.wrap_target
    pull block          ; Fetch packet payload from TX FIFO
    set pau_cfg, 3      ; Engage hardware PAU NRZI + USB Bit-Stuffing engine
    set pins, 1 [3]     ; Establish idle J-state (D-=1, D+=0) for 4 cycles

    ; Serialize Sync Byte (0x80 = 0b00000001 LSB first)
    ; PAU hardware automatically inverts line on 0s and holds on 1s
    set x, 7
sync_loop:
    out pins, 1 [2]     ; Out bit; PAU hardware drives differential D+/D-
    jmp x--, sync_loop  ; 4 cycles per bit period

    ; Serialize Data Byte payload from OSR
    set x, 7
data_loop:
    out pins, 1 [2]     ; Out bit; PAU hardware handles bit-stuffing after 6 consecutive 1s
    jmp x--, data_loop

    ; End-of-Packet (EOP) Generation:
    set pau_cfg, 0      ; Disengage PAU encoding for raw SE0 line control
    set pins, 0 [7]     ; SE0 (D-=0, D+=0) held for 2 bit times (8 cycles)
    set pins, 1 [3]     ; Return to J-state (D-=1, D+=0) for 1 bit time (4 cycles)

    irq 0               ; Packet Transmitted IRQ 0
    push noblock        ; Notify host CPU
.wrap
