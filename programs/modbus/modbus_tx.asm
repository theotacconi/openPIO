.program modbus_tx
; Modbus RTU slave transmitter on an RS-485 transceiver, the peer of modbus_rx: a reply goes out
; only after the request has ended, 3.5 characters of silence, whenever the host queued it. The
; program sets its IRQ 1 and stalls on it (irq wait); modbus_rx clears it (irq clear peer) when a
; frame ends. 11-bit characters (start, 8 data bits LSB first, 2 stop bits), 8 strobes a bit,
; the CLKDIV of modbus_rx.
; Pins: set_base TX (transceiver DI), set_base + 1 DE (driver enable, high while the reply is on
; the bus). TX FIFO, for each request in turn: N, bytes in the reply (0: no reply, a frame for
; another address), then the N bytes, one a word (bits 7:0), CRC included.
.set_pins 1 2               ; TX, DE
.out_pins 1

    set pins, 1             ; TX idle high, DE low: the bus is the master's
    set pindirs, 3
.wrap_target
top:
    irq wait 1              ; until the receiver sees the end of a request
    pull
    out x, 16
    jmp x--, reply          ; N - 1 more bytes after the first
    jmp top                 ; no reply
reply:
    set pins, 3             ; DE high, the line idle a strobe before the start bit
byte:
    pull
    set pins, 2 [6]         ; start bit: 7 strobes + set y
    set y, 7
bit:
    out pins, 1 [6]         ; data bit: 7 strobes + jmp
    jmp y--, bit
    set pins, 3 [13]        ; 2 stop bits: 14 strobes + jmp + pull, or + jmp + nop
    jmp x--, byte
    nop
    set pins, 1             ; DE low after the last stop bit
.wrap
