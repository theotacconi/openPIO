# Tiny Tapeout (IHP SG13CMOS5L) submission and hardening

Target flow, as TinyTapeout/ttihp-verilog-template branch `cmos5l`: tt-gds-action `ihp-cmos5l`, tt-support-tools `ihp-sg13cmos5l`, LibreLane 3.1.0.dev3 (action default), PDK `ihp-sg13cmos5l` (IHP-Open-PDK `2bbec755`, installed by the action from git). The organisers confirmed CMOS5L as the competition target.

## Submission files

| File | Purpose |
|---|---|
| `info.yaml` | project metadata, `top_module`, `source_files`, `tiles`, pinout (yaml_version 6) |
| `docs/info.md` | datasheet page: How it works / How to test / External hardware |
| `src/config.json` | LibreLane settings, identical to the template's `cmos5l` branch (20 ns clock, 60 % placement density, vertical PDN pitch 50 um / width 2.1 um) |
| `.github/workflows/gds.yaml` | hardening, precheck and GDS viewer jobs (template `gl_test` job omitted: no cocotb tb yet) |
| `.github/workflows/docs.yaml` | `tt_tool.py --check-docs` and datasheet PDF on every push |

### Tile size

tt-support-tools has the same tile sizes for ihp-sg13g2 and ihp-sg13cmos5l, 8x4 included. The competition allows 6x4, and 8x4 once the organisers announce it. The design does not need that much area. Utilisation below uses the SG13G2 post-CTS cell area (140,335 um^2) and must be redone with the CMOS5L figure:

| tiles | die (um) | die area (um^2) | utilisation of post-CTS cell area (140,335 um^2) |
|---|---|---|---|
| 3x2 | 636.96 x 313.74 | 199,840 | 70 % (above the 60 % placement target) |
| 4x2 | 854.40 x 313.74 | 268,061 | 52 % |
| 6x2 | 1289.28 x 313.74 | 404,499 | 35 % |
| 6x4 | 1289.28 x 710.64 | 916,214 | 15 % |

## Hardening on GitHub Actions (reference flow)

1. Enable Pages (Settings > Pages > Source: GitHub Actions) for the viewer job.
2. The `gds` workflow runs on every push:
   - **gds**: `tt_tool.py --create-user-config --ihp`, then `tt_tool.py --harden --ihp` (LibreLane). The job summary shows cell usage, utilisation, routing stats, linter output and timing.
   - **precheck**: Tiny Tapeout precheck on the final GDS (KLayout DRC, pin, boundary, layer and cell-name checks, Verilog syntax). Results in the job summary and the `precheck_reports` artifact.
   - **viewer**: 3D GDS viewer on GitHub Pages.
3. Sign-off: all jobs green, plus the `tt_submission` artifact present. `scripts/tt_signoff.py` summarises a run and fails on any DRC / LVS / antenna / slew / capacitance error or negative setup or hold slack in any corner:
   ```
   gh run download <run-id> -n GDS_logs -D /tmp/gds_logs
   python3 scripts/tt_signoff.py /tmp/gds_logs/runs/wokwi
   ```
4. The submission is made from the TT app (app.tinytapeout.com) by pointing it at the repository.

There is no local hardening flow for CMOS5L: no LibreLane container is published for 3.1.0.dev3 (checked 2026-10-24). The earlier SG13G2 script (`scripts/tt_harden.sh`) was removed; it is in git history (commit 388261d).

## Previous results (SG13G2, 6x4)

Hardened for ihp-sg13g2 before the switch to CMOS5L: LibreLane 3.0.5, IHP-Open-PDK c4b8b4e5, local run reproduced identically on GitHub Actions (commit 742bc8d, https://github.com/theotacconi/openPIO/actions/runs/39979454358) with all precheck checks passing. Kept for reference until the first CMOS5L run.

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

The slow-corner critical path (18.6 ns of the 20 ns period) starts at the fractional prescaler accumulator `u_core.div_acc`: accumulator add, period compare and `strobe` gate the whole execute datapath and the next-PC IMEM address. Registering `strobe` one cycle ahead would take it off the path if more margin is needed.
