# Agent Notes

`openPIO` is a custom cycle-accurate, 16-bit Programmable I/O (PIO) architecture targeting Tiny Tapeout (IHP 130nm CMOS5L) and physical verification on Gowin FPGA (Tang Nano 20K). It is not a soft CPU or a generic microcontroller. The goal is a razor-sharp, zero-jitter hardware datapath capable of deterministic serial protocol emulation in under 64 instructions per routine.

## Goals

* Entry for the protocol emulator ASIC competition (`COMPETITION.md`), due **2027-01-18**: public repository with RTL, tests, a passing Tiny Tapeout GDS and precheck, the architecture and ISA documentation, the assembler with example programs, a write-up of the verification and of how AI was used.
* Judging: flexibility first (the chip runs protocols it was not designed for), then the protocols shown, what the architecture makes possible, and how it was designed and verified.
* Beyond the competition: a protocol emulator useful in the real world (robotics, automation, debugging), on silicon and on FPGA. The measure is flexibility: ideally any existing serial protocol can be written for it, and the more protocols the programs prove, the better.
* Priorities, in order: correctness and timing determinism, closing timing and area, features. Every instruction takes a known number of clocks: no unmodeled latency or branch penalty.
* The ISA is v2 and frozen until the v2 RTL passes lockstep against the model. Every spec feature is exercised by at least one program; a feature without one gets a program, not silence.
* Sizes that the silicon fixes but an FPGA could grow (number of SMs, FIFO depth) are RTL parameters, set for the chip in its top level.
* Silicon: 6x4 tiles (8x4 if the organisers release it), 50 MHz (20 ns) closing setup and hold in all three corners. Measure the cell area of any addition (Yosys + CMOS5L Liberty) before committing to it.

## Reference model

* `sim/model/` is the specification: `ISA.md` and `HOST.md` in prose, `emu.py` (one SM) and `chip.py` (SMs, shared pins, host registers, SPI) executable, `asm.py` the assembler, `host.py` the SPI driver.
* The RTL matches `chip.py` clock for clock on the pins and on every host-visible register (`ISA.md`, Timing within a clock). When they disagree the RTL is wrong, unless the model is: then fix spec, model and tests together, in one commit.
* Spec changes (`ISA.md`, `HOST.md` and the model's behaviour) are the user's decision: propose them with their cost and benefit, do not make them unasked.

## Hardware rules

* Every flip-flop has a synchronous active-low reset (`rst_n`), except memory arrays: the IMEM and the FIFO SRAM, and their flip-flop models, which must not be reset either. The host writes every IMEM word it executes.
* Hard macros only behind a module with a portable flip-flop model of the same ports, cycle-equivalent while the SM runs (`imem_sram.v` / `imem.v`). The model is used by RTL simulation, formal, lockstep and the FPGA; `info.yaml` lists the macro version. Macros: IMEM `RM_IHPSG13_1P_64x16_c2`, FIFO data `RM_IHPSG13_1P_256x16_c2_bm_bist` (BIST and bit-mask pins tied off); CMOS5L uses the SG13G2 SRAM library. Each one goes in `src/config.json` (`MACROS`, `PDN_MACRO_CONNECTIONS`); `odb_stripes.py` powers every macro. No other vendor primitives.
* The number of SMs is a parameter (the model allows 2 or 4); the chip has 2 unless 4 fit.
* The PAU holds generic primitives only. A feature joins it when (1) it is generic: parameters, not a protocol's format; (2) two real protocols, or a family, use it; (3) the SM cannot do it in software at the speed those protocols need; (4) its measured area is small. Protocol-specific encodings (a field layout, a Gray code) stay in programs.
* The ISA has 16 pin positions; the top level maps them to pads and reports the map in PADS_OUT / PADS_IN / PADS_BIDIR (HOST.md, Pins). The core never knows which pads exist.
* No combinational path from an input pin to an output pin: inputs pass the 2-flip-flop synchronizer.
* Timing lessons from the v1 hardening (CMOS5L slow corner, 20 ns):
  * The SRAM's clock-to-output (up to 5.3 ns) starts the critical path: keep the logic from the IMEM output to the next PC short. The four condition values depend only on state, not on the instruction: compute them while the IMEM reads, so the instruction only selects one.
  * Register wide comparisons (CRC == CRC_RES) and the prescaler strobe (one clock ahead, v1 `fb87441`, proven in `test/formal/core_strobe.sby`).
  * No ripple chains over the bits of a word: v1's NRZI / stuffing encoder had to be rewritten as parallel per-bit windows (`c797771`, proven in `test/formal/nrzi_equiv.sby`).
  * High-fanout enables need the wire-length repair of `src/config.json`; design repair checks slew only in the typical corner.

## Quality Rules

* Write clean, idiomatic, fully synthesizable Verilog-2001. No SystemVerilog constructs that break Yosys or open-source toolchains.
* Do not introduce slop: avoid speculative wrapper glue, dead ports, fake parameterized features that will never be used, workarounds that patch bugs in tests instead of fixing the root cause in the datapath, fragile code that only patches specific cases, dead or useless code, and code far more complicated than it needs to be.
* Keep the implementation small, sharp, easy to understand. Try to write elegant code in a state of grace. Don't settle for the first thing that comes to mind, try to find the most minimal and better working design.
* Prefer comments beside the implementation over separate design documents.
* Code comments must be terse, dense, and physically informative: annotate clock edges, setup/hold cycles, protocol bit phases, and non-obvious mux priorities directly in the RTL.

## Git & Commit Discipline

* Make small, atomic, logical commits. Do not lump multiple unrelated modules, bug fixes, or refactors into a single massive commit.
* Commit messages follow conventional commits:
  * `feat(alu): implement 16-bit REV and ADD logic`
  * `fix(shifter): prevent OSR underflow on 16-bit shift count`
  * `test(uart): add cocotb framing error testbench`
  * `docs(agent): update pin mapping interface`
* Always run the linting check and relevant unit/lockstep tests **before** staging and committing files.
* Never commit scratch virtualenvs, compilation artifacts (`*.pyc`, `*.vcd`, `*.vvp`, `obj_dir/`), or temporary dump files. Keep `.gitignore` strictly honored.

## Layout

* `sim/model/`: the specification and reference model (above).
* `sim/learn/`: `learn.py` finds a protocol's parameters from a recording of the wire and writes the programs that speak it (async serial so far); `test/learn/` checks it against the reference devices.
* `programs/<protocol>/`: `*.asm` programs, each with a `test_*.py` that runs it on the chip model; `programs/README.md` is generated by `programs/report.py`.
* `test/shared/`: `devices.py`, reference devices written from the protocol specifications (UART, SPI, I2C, USB, CAN, ...), `bench.py` the pin-level bench, `test_devices.py` their own tests.
* `test/model/`: tests of the ISA, the assembler and the chip model.
* `src/`: RTL, Tiny Tapeout top `tt_um_openpio.v`; `src/config.json` and `src/pdn_cfg.tcl` with `librelane_plugin_openpio_pdn.py` and `odb_stripes.py` (repository root, where LibreLane finds them) configure the hardening.
* `test/test.py`, `test/tb.v`, `test/Makefile`: cocotb tests of the chip through its pins (RTL, SRAM model, gate level). `test/lockstep/`: the RTL as a Yosys netlist stepped against the model every clock. `test/formal/`: SymbiYosys proofs.
* `fpga/tang_nano_20k/`: FPGA wrapper and build.
* `docs/`: `info.md` (Tiny Tapeout datasheet, required by the docs and gds workflows), `diagrams.py` and the SVGs it generates.
* `scripts/`: `dev.sh` (container), `area.sh` (CMOS5L cell area of a design), `tt_signoff.py` (checks a gds run's logs).
* `.github/workflows/`: `model` (Python model and programs), `test` (cocotb on the RTL), `gds` (hardening, precheck, gate-level test, sign-off), `docs`.

Status (2026-10-28): ISA v2 frozen after the slimming; the model, the 31 programs, the learner (async serial) and the docs are v2. `src/`, `test/test.py`, `test/lockstep/`, `test/formal/` and `fpga/` are still v1 and are ported together with the RTL; until then the `test` workflow fails on this branch.

## Testing

* Model and programs (any change to `sim/model/`, `programs/` or `test/shared/`):
  ```
  for t in test/model/test_*.py; do python3 "$t"; done
  (cd test/shared && python3 test_devices.py)
  python3 test/learn/test_learn.py
  python3 programs/report.py      # commit the regenerated programs/README.md: CI checks it
  ```
* A program is judged only by its pins and the data it exchanges with the host, by the reference devices, never by the emulator's internal state. A new spec feature comes with a program that uses it.
* RTL (inside the container, `scripts/dev.sh`):
  1. Lint: `yosys -p "read_verilog <src/*.v but imem_sram.v>; hierarchy -check -top tt_um_theotacconi_openpio; proc; check -assert"` and `verilator --lint-only -Wall`: no latches, no multiple drivers, no warnings.
  2. Lockstep against the model: `python3 test/lockstep/fuzz.py` (random programs and host traffic) and the program replays in `test/lockstep/`.
  3. cocotb through the pins: `cd test && make -B` (and `IMEM=sram` when the IMEM or FIFO memories change).
  4. Formal: `sby -f test/formal/<proof>.sby` for every invariant or equivalence a timing fix relies on.
* Sign-off: the `gds` workflow green (gds, precheck, gl_test, signoff, viewer) and the `tt_submission` artifact present. `scripts/tt_signoff.py` fails on any DRC / LVS / antenna / slew / capacitance error or negative slack in any corner.
