.program ps2_rx
; PS/2 Keyboard & Mouse Protocol Receiver
; Pin 0: CLK (driven by PS/2 device, open-drain with pull-up)
; Pin 1: DATA (driven by PS/2 device, open-drain with pull-up, in_base=1)
; Frame format: 1 Start bit (0), 8 Data bits (LSB-first), 1 Odd Parity bit, 1 Stop bit (1)
; Data is sampled on CLK falling edges.

.wrap_target
    ; 1. Wait for Start Bit (falling CLK edge, DATA must be low)
    wait 0 gpio 0
    wait 1 gpio 0

    ; 2. Sample 8 Data Bits into ISR
    set x, 7
data_loop:
    wait 0 gpio 0       ; Wait for CLK falling edge
    in pins, 1          ; Sample DATA bit into ISR
    wait 1 gpio 0       ; Wait for CLK rising edge
    jmp x--, data_loop  ; Loop for 8 data bits

    ; 3. Sample Odd Parity Bit
    wait 0 gpio 0
    in pins, 1          ; Sample Parity bit into ISR
    wait 1 gpio 0

    ; 4. Await Stop Bit (CLK falling then rising, line high)
    wait 0 gpio 0
    wait 1 gpio 0

    irq 0               ; Frame received IRQ 0
    push noblock        ; Push 9-bit payload (8 data + 1 parity) to RX FIFO
.wrap
