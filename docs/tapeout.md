# Tiny Tapeout (IHP SG13CMOS5L) submission and hardening

Target flow, as TinyTapeout/ttihp-verilog-template branch `cmos5l`: tt-gds-action `ihp-cmos5l`, tt-support-tools `ihp-sg13cmos5l`, LibreLane 3.1.0.dev3 (action default), PDK `ihp-sg13cmos5l` (IHP-Open-PDK `2bbec755`, installed by the action from git). The organisers confirmed CMOS5L as the competition target.

## Submission files

| File | Purpose |
|---|---|
| `info.yaml` | project metadata, `top_module`, `source_files`, `tiles`, pinout (yaml_version 6) |
| `docs/info.md` | datasheet page: How it works / How to test / External hardware |
| `src/config.json` | LibreLane settings: the template's `cmos5l` branch (20 ns clock, 60 % placement density, vertical PDN pitch 50 um / width 2.1 um) plus the slew repair settings and the SRAM macro (below) |
| `src/pdn_cfg.tcl`, `librelane_plugin_openpio_pdn.py`, `odb_stripes.py` | power grid for the SRAM macro (see IMEM SRAM macro) |
| `.github/workflows/gds.yaml` | hardening, sign-off, gate-level test, precheck and GDS viewer jobs |
| `.github/workflows/test.yaml` | cocotb tests (`test/`) on the RTL |
| `.github/workflows/docs.yaml` | `tt_tool.py --check-docs` and datasheet PDF on every push |

### Tile size

tt-support-tools has the same tile sizes for ihp-sg13g2 and ihp-sg13cmos5l, 8x4 included. The competition allows 6x4, and 8x4 once the organisers announce it. The design does not need that much area. Utilisation below uses the CMOS5L final area with the SRAM IMEM: 93,854 um^2 of standard cells (hold and repair buffers included) plus the 15,240 um^2 macro, 109,094 um^2:

| tiles | die (um) | die area (um^2) | utilisation of 109,094 um^2 |
|---|---|---|---|
| 3x2 | 636.96 x 313.74 | 199,840 | 55 % |
| 4x2 | 854.40 x 313.74 | 268,061 | 41 % |
| 6x2 | 1289.28 x 313.74 | 404,499 | 27 % |
| 6x4 | 1289.28 x 710.64 | 916,214 | 12 % |

## Hardening on GitHub Actions (reference flow)

1. Enable Pages (Settings > Pages > Source: GitHub Actions) for the viewer job.
2. The `gds` workflow runs on pushes that change `src/`, `info.yaml`, the workflow, `scripts/tt_signoff.py` or the PDN plugin files, and by hand (Actions > gds > Run workflow):
   - **gds**: `tt_tool.py --create-user-config --ihp`, then `tt_tool.py --harden --ihp` (LibreLane). The job summary shows cell usage, utilisation, routing stats, linter output and timing.
   - **precheck**: Tiny Tapeout precheck on the final GDS (KLayout DRC, pin, boundary, layer and cell-name checks, Verilog syntax). Results in the job summary and the `precheck_reports` artifact.
   - **viewer**: 3D GDS viewer on GitHub Pages.
   - **gl_test**: the cocotb tests of `test/` on the hardened gate-level netlist (TT action).
   - **signoff**: `scripts/tt_signoff.py` on the `GDS_logs` artifact, summary in the job summary. Fails on any DRC / LVS / antenna / slew / capacitance error or negative setup or hold slack in any corner: the gds job alone stays green on timing failures.
3. Sign-off: all jobs green, plus the `tt_submission` artifact present. To rerun the sign-off script locally on a run:
   ```
   gh run download <run-id> -n GDS_logs -D /tmp/gds_logs
   python3 scripts/tt_signoff.py /tmp/gds_logs/runs/wokwi
   ```
4. The submission is made from the TT app (app.tinytapeout.com) by pointing it at the repository. Run `gds` by hand on the commit to submit first if it only changed docs: the `tt_submission` artifact includes `docs/`.

There is no local hardening flow for CMOS5L: no LibreLane container is published for 3.1.0.dev3 (checked 2026-10-24). The earlier SG13G2 script (`scripts/tt_harden.sh`) was removed; it is in git history (commit 388261d).

## Results (SG13CMOS5L, 6x4, SRAM IMEM)

Commit 359c7ff (branch sram-imem), https://github.com/theotacconi/openPIO/actions/runs/40483141307: all jobs green (gds, precheck, gl_test 5/5, signoff, viewer), `scripts/tt_signoff.py` passes.

| Check | Result |
|---|---|
| Detailed-route DRC | 0 |
| Magic DRC | not run (see IMEM SRAM macro) |
| KLayout SG13CMOS5L DRC (precheck, on the full GDS with the SRAM) | pass |
| LVS (netgen, SRAM as a black box): errors / unmatched pins | 0 / 0 ("Circuits match uniquely") |
| Antenna violating nets / pins | 0 / 0 |
| Max slew / max cap violations | 0 / 0 |
| IR drop, worst case VPWR / VGND | 0.16 mV / 0.15 mV |
| Precheck: pins, boundary, layers, cell names, zero area, label overlap | pass |

| Corner | Setup WNS | Hold WNS |
|---|---|---|
| nom_slow_1p08V_125C | +2.335 ns | +0.578 ns |
| nom_typ_1p20V_25C | +9.007 ns | +0.270 ns |
| nom_fast_1p32V_m40C | +12.869 ns | +0.094 ns |

7,440 standard cells (93,854 um^2) plus the SRAM (15,240 um^2), 663 hold buffers, 2.0 mW total power (LibreLane power report, 50 MHz), 403 mm routed wire. The slow-corner critical path (18.0 ns of the 20 ns period) starts at the SRAM output (`u_core.u_imem.u_sram`, clock to A_DOUT up to 5.3 ns in that corner).

Against the flip-flop IMEM (commit fbaf574, run 40218780632: 11,046 cells, 151,609 um^2, 1,181 hold buffers, 3.8 mW, slow setup +4.530 ns / hold +0.443 ns, critical path `u_core.dbg_instr` back into itself, 15.8 ns): -28 % area, -47 % power, -2.2 ns slow-corner setup margin.

### IMEM SRAM macro

The instruction memory is IHP's `RM_IHPSG13_1P_64x16_c2` (64 x 16, single port, 236.80 x 64.36 um, at (3.36, 3.78) um, orientation FS, pins facing the logic), instantiated by `src/imem_sram.v` (module `imem`, words 0..31 used). The recipe follows UW-ASIC/TinyTracer (same action and PDK commit), which took it from kdp1965/ihp-um-janestreet-prism:

- `MACROS` and `PDN_MACRO_CONNECTIONS` (`VDD!`, `VDDARRAY!` on VPWR, `VSS!` on VGND) in `src/config.json`; `src/sram_blackbox.v` declares the macro's ports for the Verilator lint (the flow otherwise knows it only from `MACROS`).
- Power: the tile grid is Metal4 stripes only and the macro blocks Metal2-Metal4, so pdngen stops at it. `src/pdn_cfg.tcl` drops LibreLane's empty per-macro grid, and the `Project.ExtendPowerStripes` step (`librelane_plugin_openpio_pdn.py`, loaded by LibreLane from the repository root, running `odb_stripes.py`) moves the 5+5 tile stripes over the macro onto its supply columns and adds one pair over its periphery band: 2 VPWR/VGND pairs in each region (left array, band, right array). `ERROR_ON_PDN_VIOLATIONS` is off because GeneratePDN checks the grid before that step; LVS and the IR drop report check the result.
- Sign-off: Magic DRC and its illegal-overlap check are off (false overlaps on the macro LEF's Metal4 obstruction at the `VDD!`/`VDDARRAY!` break, not in its GDS); the precheck's KLayout DRC on the real GDS is the DRC sign-off. LVS extracts the macro as an abstract cell (`MAGIC_EXT_ABSTRACT_CELLS`).

### Timing closure history

The first CMOS5L run (40120003681, RTL unchanged from SG13G2) failed sign-off: setup -1.003 ns and 7 max-slew violations in nom_slow_1p08V_125C.

- Setup, `u_core.div_acc -> dbg_instr`: prescaler add, period compare and `strobe` gated the whole execute datapath. Fixed by registering `strobe` one clock ahead (fb87441; invariant proven in `test/formal/core_strobe.sby`).
- Setup, `dbg_instr -> uio_oe` (-0.23 ns): the NRZI / bit-stuffing encoder was a 16-stage ripple over the OUT bits. Fixed by evaluating each bit from its own run window (c797771; equivalence to the per-bit loop proven in `test/formal/nrzi_equiv.sby`).
- Max slew: design repair (`repair_design`) only checks the typical corner, where slews are up to 1.66x smaller than in the slow corner for the same 2.507 ns limit. `DESIGN_REPAIR_MAX_SLEW_PCT` 50 plus a post-global-routing repair (a635a26) fixed 2 of 3 nets. The last one, the execute enable (`en & strobe & delay_cnt == 0`, one nand4_1 driving 8 loads over 871 um), escaped both slew-based passes; `DESIGN_REPAIR_MAX_WIRE_LENGTH` / `GRT_DESIGN_REPAIR_MAX_WIRE_LENGTH` 300 um buffers it (fbaf574).

## Previous results (SG13G2, 6x4)

Hardened for ihp-sg13g2 before the switch to CMOS5L: LibreLane 3.0.5, IHP-Open-PDK c4b8b4e5, local run reproduced identically on GitHub Actions (commit 742bc8d, https://github.com/theotacconi/openPIO/actions/runs/39979454358) with all precheck checks passing.

| Check | Result |
|---|---|
| Detailed-route DRC | 0 |
| Magic DRC | 0 |
| KLayout SG13G2 DRC (precheck, 33 rule groups) | 0 |
| LVS (netgen): errors / unmatched nets, devices, pins | 0 / 0 / 0 / 0 |
| Antenna violating nets / pins | 0 / 0 |
| Max slew / max cap violations | 0 / 0 |
| Precheck: pins, boundary, layers, cell names, zero area, label overlap, Verilog syntax | pass |

| Corner | Setup WNS | Hold WNS |
|---|---|---|
| nom_slow_1p08V_125C | +1.438 ns | +0.626 ns |
| nom_typ_1p20V_25C | +8.260 ns | +0.292 ns |
| nom_fast_1p32V_m40C | +12.243 ns | +0.113 ns |

9,849 standard cells (140,335 um^2, 15.6 % of the 6x4 core), 1,054 hold buffers, 3.8 mW total power (LibreLane power report, 50 MHz). Global routing overflow 0.

The slow-corner critical path (18.6 ns of the 20 ns period) started at the fractional prescaler accumulator `u_core.div_acc`; CMOS5L made it fail (see Timing closure history).
