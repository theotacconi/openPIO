.program manchester_tx
; Manchester (IEEE 802.3: 1 = rising edge at mid-bit) on the PAU (ISA v2)
; Pin 0: TX. 6 strobes per bit (PAU half-bit 3), MSB first. The PAU keeps the bit timing and
; autopull refills the OSR from the TX FIFO in the same strobe: one instruction streams every
; word pushed by the host, without gaps.
.pau enc=manchester baud=3

    set pins, 0
    set pindirs, 1
    set pau_ctl, tx
.wrap_target
    out pins, 16 auto
.wrap
