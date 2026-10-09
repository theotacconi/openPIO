.program swd_host
; ARM Serial Wire Debug (SWD) Host Protocol Controller
; Pin 0: SWCLK (side-set 1, clock driven by host)
; Pin 1: SWDIO (bidirectional data line: out_base=1, in_base=1, set_base=0)
; Features:
;   - Host Request Serialization (8 bits)
;   - Turnaround Cycle (Trn: exactly 1 clock cycle line float)
;   - Target ACK Sampling (3 bits: OK=001b)
;   - Turnaround Cycle (Trn: return line to host output)

.side_set 1
.wrap_target
    pull block side 0       ; Fetch request byte from TX FIFO; SWCLK idle low
    set pindirs, 2 side 0   ; Assert SWDIO as output (pindirs bit 1 = 1)
    set x, 7 side 0         ; 8 request bits

req_loop:
    out pins, 1 side 0 [1]  ; SWCLK low, drive SWDIO bit (hold 2 cycles)
    nop side 1 [1]          ; SWCLK high (hold 2 cycles)
    jmp x--, req_loop side 0 ; Total 5 cycles per bit

    ; Turnaround Cycle (Trn): Switch SWDIO from Output to Input
    set pindirs, 0 side 0 [1] ; SWDIO line released (high-Z)
    nop side 1 [1]          ; SWCLK rising edge during turnaround

    ; Target ACK Phase: Sample 3 bits from Target
    set x, 2 side 0         ; 3 ACK bits (2 down to 0)
ack_loop:
    nop side 0 [1]          ; SWCLK low
    in pins, 1 side 1 [1]   ; SWCLK high: sample Target SWDIO bit
    jmp x--, ack_loop side 0 ; Next ACK bit

    ; Turnaround Cycle (Trn): Switch back to Host Output
    nop side 0 [1]          ; SWCLK low
    set pindirs, 2 side 1 [1] ; Re-assert SWDIO output
    push noblock side 0     ; Push 3-bit ACK to RX FIFO
.wrap
