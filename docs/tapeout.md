# Tiny Tapeout (IHP SG13G2) submission and hardening

Target flow: TinyTapeout/tt-gds-action `ttihp26b`, tt-support-tools `main`, LibreLane 3.0.5, PDK `ihp-sg13g2`. Note that TT's IHP flow uses SG13G2, not SG13CMOS5L; `ihp-sg13g2` is the only IHP value tt-gds-action accepts.

## Submission files

| File | Purpose |
|---|---|
| `info.yaml` | project metadata, `top_module`, `source_files`, `tiles`, pinout (yaml_version 6) |
| `docs/info.md` | datasheet page: How it works / How to test / External hardware |
| `src/config.json` | LibreLane settings, copied unchanged from ttihp-verilog-template (20 ns clock, 60 % placement density) |
| `.github/workflows/gds.yaml` | hardening, precheck and GDS viewer jobs (template `gl_test` job omitted: no cocotb tb yet) |
| `.github/workflows/docs.yaml` | datasheet build check |

Checked locally with tt-support-tools `main`:

- `tt_tool.py --ihp --print-top-module`: `info.yaml` passes `ProjectInfo` validation.
- `tt_tool.py --ihp --check-docs`: passes.
- `tt_tool.py --ihp --create-user-config`: writes `src/config_merged.json` (die `0 0 1289.28 710.64` for 6x4).

### Tile size

`tiles: "6x4"` is valid for ihp-sg13g2 in tt-support-tools (the template comment only lists sizes up to 8x2). The design does not need that much area:

| tiles | die (um) | die area (um^2) | utilisation of post-CTS cell area (140,335 um^2) |
|---|---|---|---|
| 3x2 | 636.96 x 313.74 | 199,840 | 70 % (above the 60 % placement target) |
| 4x2 | 854.40 x 313.74 | 268,061 | 52 % |
| 6x2 | 1289.28 x 313.74 | 404,499 | 35 % |
| 6x4 | 1289.28 x 710.64 | 916,214 | 15 % |

4x2 is the smallest size with headroom under the 60 % density target; it must be confirmed by a hardening run before changing `info.yaml`.

## Hardening on GitHub Actions (reference flow)

1. Push the repository to GitHub and enable Pages (Settings > Pages > Source: GitHub Actions) for the viewer job.
2. The `gds` workflow runs on every push:
   - **gds**: `tt_tool.py --create-user-config --ihp`, then `tt_tool.py --harden --ihp` (LibreLane). The job summary shows cell usage, utilisation, routing stats, linter output and timing.
   - **precheck**: `tt/precheck/precheck.py --gds tt_submission/*.gds --tech ihp-sg13g2` in the precheck nix shell: Magic DRC, KLayout FEOL/BEOL/offgrid/SG13G2 DRC, pin and boundary checks against the tile template, LEF/DEF/Verilog consistency. Results in the job summary (`reports/results.md`).
   - **viewer**: 3D GDS viewer on GitHub Pages.
3. Sign-off: both jobs green, plus the gds artifact (`tt_submission/`) present. The submission is made from the TT app (app.tinytapeout.com) by pointing it at the repository.

## Hardening locally

`scripts/tt_harden.sh` runs the same steps on a workstation:

```
scripts/tt_harden.sh                 # docker, or rootless podman if docker is absent
ENGINE=podman PYTHON=python3.12 scripts/tt_harden.sh
```

1. Clones tt-support-tools into `./tt` (ref `TT_TOOLS_REF`, default `main`), creates `tt/.venv` and runs `--check-docs` and `--create-user-config`.
2. Runs LibreLane inside `ghcr.io/librelane/librelane:3.0.5` (5.8 GB). On first use, LibreLane fetches the ihp-sg13g2 PDK into `$PDK_ROOT` (default `~/.ciel`). This is equivalent to `tt_tool.py --harden --ihp`, which needs a host LibreLane install plus docker.
3. Runs `scripts/tt_signoff.py runs/wokwi`, which reads `runs/wokwi/final/metrics.json` and fails on any non-zero DRC / LVS / antenna / slew / capacitance count or negative setup or hold slack in any corner.

Outputs (gitignored): `runs/wokwi/` (all step logs and reports; `final/gds`, `final/metrics.json`), `tt/`.

Notes:
- tt-support-tools pins `numpy==1.26.4`, which has no wheel for Python >= 3.13; the script then installs the same packages unpinned. `PYTHON=python3.12` avoids this.
- tt_tool runs `yowasp-yosys`, which mounts its own temporary directory over `/tmp`; keep the checkout outside `/tmp`.

### Results (6x4, local run, LibreLane 3.0.5, IHP-Open-PDK c4b8b4e5)

Reproduced identically on GitHub Actions (commit 742bc8d, same LibreLane and PDK), with all precheck checks passing: https://github.com/theotacconi/openPIO/actions/runs/39979454358

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

### Precheck locally (optional)

Precheck needs Nix, like CI:

```
python tt/tt_tool.py --ihp --create-tt-submission
cd tt/precheck && nix-shell --run \
  "PDK=ihp-sg13g2 PDK_ROOT=$HOME/.ciel python precheck.py --gds ../../tt_submission/tt_um_theotacconi_openpio.gds --tech ihp-sg13g2"
```

CI pins the precheck PDK to ihp-sg13g2 commit `22f43352dd8219f9007eb659e422e0d5fe28c5fb` (tt-gds-action `precheck/action.yml`).
