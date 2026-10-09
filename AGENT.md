# Agent Notes

`openPIO` is a custom cycle-accurate, 16-bit Programmable I/O (PIO) architecture targeting Tiny Tapeout (IHP 130nm CMOS5L) and physical verification on Gowin FPGA (Tang Nano 20K). It is not a soft CPU or a generic microcontroller. The goal is a razor-sharp, zero-jitter hardware datapath capable of deterministic serial protocol emulation in under 32 instructions per routine.

## Goals

* Prioritize hardware correctness, timing determinism, and silicon budget over software abstractions.
* Preserve zero-bubble cycle execution: branch decisions and register writebacks must never introduce unmodeled pipeline latency or branch-penalty jitter.
* Keep the hardware synthesizable strictly under Verilog-2001. Ensure clean, warning-free synthesis with both open-source Yosys/OpenLane (for Tiny Tapeout) and Gowin EDA (for Tang Nano 20K).
* Strictly obey the Tiny Tapeout 6x4 tile envelope: preserve low flip-flop and gate counts (keep core registers + IMEM well within the ~1.5k DFF ceiling).
* Treat `sim/model/emu.py` and `sim/model/ISA.md` as the frozen Golden Architectural Reference. RTL must track emulator semantics with zero drift.
* Maintain comprehensive co-simulation: every instruction and protocol routine must run against lockstep verification in cocotb.

## Quality Rules

* Write clean, idiomatic, fully synthesizable Verilog-2001. No SystemVerilog constructs that break Yosys or open-source toolchains.
* Zero combinatorial loops and zero unclocked latches. Every flip-flop must have an explicit, synchronous active-low reset (`rst_n`).
* Registered I/O boundary: no purely combinatorial paths from input pins (`gpio_in`) directly through the ALU to output pins (`gpio_out`) in the same machine cycle.
* Do not introduce slop: avoid speculative wrapper glue, dead ports, fake parameterized features that will never be used, or workarounds that patch bugs in tests instead of fixing the root cause in the datapath.
* Code comments must be terse, dense, and physically informative: annotate clock edges, setup/hold cycles, protocol bit phases, and non-obvious mux priorities directly in the RTL.
* Do not introduce vendor-specific hard primitives (e.g., hard Gowin BRAM or IHP macros) into core modules. The core must infer standard standard-cell flip-flops and logic.

## Verification & Golden Model Invariance

* `sim/model/` is the single source of architectural truth. If an RTL implementation contradicts `sim/model/emu.py`, **the RTL is wrong**, not the emulator.
* Never modify `ISA.md`, `asm.py`, or `emu.py` without explicit written confirmation from the user.
* All new features, protocol drivers, and edge cases must first be proven in `emu.py` before touching Verilog.
* Do not commit RTL that does not pass linting (`yosys -p "prep; check"`) and equivalence checks against the golden model.

## Git & Commit Discipline

* Make small, atomic, logical commits. Do not lump multiple unrelated modules, bug fixes, or refactors into a single massive commit.
* Commit messages must follow conventional commits:
* `feat(alu): implement 16-bit REV and ADD logic`
* `fix(shifter): prevent OSR underflow on 16-bit shift count`
* `test(uart): add cocotb framing error testbench`
* `docs(claude): update pin mapping interface`


* Always run the linting check and relevant unit/lockstep tests **before** staging and committing files.
* Never commit scratch virtualenvs, compilation artifacts (`*.pyc`, `*.vcd`, `*.vvp`, `obj_dir/`), or temporary dump files. Keep `.gitignore` strictly honored.

## Layout

* `src/`: synthesizable Verilog-2001 source files (`alu.v`, `shifter.v`, `imem.v`, `openpio_core.v`, etc.).
* `sim/model/`: authoritative golden reference model (`emu.py`), reference assembler (`asm.py`), and frozen specification (`ISA.md`).
* `test/`: cocotb testbenches, test runners, and lockstep co-simulation wrappers.
* `agents/`: verified reference protocol firmware programs (`*.asm`), test harnesses, and validation reports.
* `info.yaml`: Tiny Tapeout configuration, pin multiplexing, and top-level tile declarations.
* `prompts/`: architectural prompts, multi-agent orchestrator templates, and specs.

## Testing & Sign-Off Checklist

Before considering any task or module complete, you must:

1. Run standard linting and syntax checking via Yosys (`hierarchy -check`, `check -assert`). Ensure 0 synthesis warnings regarding inferred latches or multi-driven nets.
2. Verify module output against `sim/model/emu.py` with randomized input vectors.
3. When modifying core datapath or execution logic, re-run all 14 reference protocol test suites (`agents/*/test_*.py`) to guarantee zero regressions.
4. Ensure IMEM dual-port memory semantics remain respected: 1-cycle synchronous read latency with zero-cycle combinatorial next-PC feedforward.
5. Create a descriptive, scoped git commit once verification passes.
