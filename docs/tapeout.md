# Tiny Tapeout (IHP SG13CMOS5L) submission and hardening

Target flow, as TinyTapeout/ttihp-verilog-template branch `cmos5l`: tt-gds-action `ihp-cmos5l`, tt-support-tools `ihp-sg13cmos5l`, LibreLane 3.1.0.dev3 (action default), PDK `ihp-sg13cmos5l` (IHP-Open-PDK `2bbec755`, installed by the action from git). The organisers confirmed CMOS5L as the competition target.

## Submission files

| File | Purpose |
|---|---|
| `info.yaml` | project metadata, `top_module`, `source_files`, `tiles`, pinout (yaml_version 6) |
| `docs/info.md` | datasheet page: How it works / How to test / External hardware |
| `src/config.json` | LibreLane settings: the template's `cmos5l` branch (20 ns clock, 60 % placement density, vertical PDN pitch 50 um / width 2.1 um) plus the slew repair settings below |
| `.github/workflows/gds.yaml` | hardening, precheck and GDS viewer jobs (template `gl_test` job omitted: no cocotb tb yet) |
| `.github/workflows/docs.yaml` | `tt_tool.py --check-docs` and datasheet PDF on every push |

### Tile size

tt-support-tools has the same tile sizes for ihp-sg13g2 and ihp-sg13cmos5l, 8x4 included. The competition allows 6x4, and 8x4 once the organisers announce it. The design does not need that much area. Utilisation below uses the CMOS5L final standard-cell area (151,609 um^2, hold and repair buffers included):

| tiles | die (um) | die area (um^2) | utilisation of final cell area (151,609 um^2) |
|---|---|---|---|
| 3x2 | 636.96 x 313.74 | 199,840 | 76 % (above the 60 % placement target) |
| 4x2 | 854.40 x 313.74 | 268,061 | 57 % |
| 6x2 | 1289.28 x 313.74 | 404,499 | 37 % |
| 6x4 | 1289.28 x 710.64 | 916,214 | 17 % |

## Hardening on GitHub Actions (reference flow)

1. Enable Pages (Settings > Pages > Source: GitHub Actions) for the viewer job.
2. The `gds` workflow runs on pushes that change `src/`, `info.yaml`, the workflow or `scripts/tt_signoff.py`, and by hand (Actions > gds > Run workflow):
   - **gds**: `tt_tool.py --create-user-config --ihp`, then `tt_tool.py --harden --ihp` (LibreLane). The job summary shows cell usage, utilisation, routing stats, linter output and timing.
   - **precheck**: Tiny Tapeout precheck on the final GDS (KLayout DRC, pin, boundary, layer and cell-name checks, Verilog syntax). Results in the job summary and the `precheck_reports` artifact.
   - **viewer**: 3D GDS viewer on GitHub Pages.
   - **signoff**: `scripts/tt_signoff.py` on the `GDS_logs` artifact, summary in the job summary. Fails on any DRC / LVS / antenna / slew / capacitance error or negative setup or hold slack in any corner: the gds job alone stays green on timing failures.
3. Sign-off: all jobs green, plus the `tt_submission` artifact present. To rerun the sign-off script locally on a run:
   ```
   gh run download <run-id> -n GDS_logs -D /tmp/gds_logs
   python3 scripts/tt_signoff.py /tmp/gds_logs/runs/wokwi
   ```
4. The submission is made from the TT app (app.tinytapeout.com) by pointing it at the repository. Run `gds` by hand on the commit to submit first if it only changed docs: the `tt_submission` artifact includes `docs/`.

There is no local hardening flow for CMOS5L: no LibreLane container is published for 3.1.0.dev3 (checked 2026-10-24). The earlier SG13G2 script (`scripts/tt_harden.sh`) was removed; it is in git history (commit 388261d).

## Results (SG13CMOS5L, 6x4)

Commit fbaf574, https://github.com/theotacconi/openPIO/actions/runs/40218780632: all jobs green, all precheck checks passing, `scripts/tt_signoff.py` passes.

| Check | Result |
|---|---|
| Detailed-route DRC | 0 |
| Magic DRC | 0 |
| KLayout SG13CMOS5L DRC (precheck) | pass |
| LVS (netgen): errors / unmatched pins | 0 / 0 |
| Antenna violating nets / pins | 0 / 0 |
| Max slew / max cap violations | 0 / 0 |
| Precheck: pins, boundary, layers, cell names, zero area, label overlap, analog pins | pass |

| Corner | Setup WNS | Hold WNS |
|---|---|---|
| nom_slow_1p08V_125C | +4.530 ns | +0.443 ns |
| nom_typ_1p20V_25C | +10.132 ns | +0.187 ns |
| nom_fast_1p32V_m40C | +13.106 ns | +0.039 ns |

11,046 standard cells (151,609 um^2, 16.8 % of the 6x4 core), 1,181 hold buffers, 3.8 mW total power (LibreLane power report, 50 MHz), 493 mm routed wire. Global routing overflow 0. The slow-corner critical path (15.8 ns of the 20 ns period) runs from the instruction register `u_core.dbg_instr` back into it.

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
