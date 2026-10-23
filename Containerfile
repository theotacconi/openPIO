# openPIO development environment: lint, simulation, formal and FPGA tools.
# Same base as the GitHub Actions runners (ubuntu-24.04). Hardening (LibreLane)
# runs in its own image, see scripts/tt_harden.sh.
#
#   scripts/dev.sh [command]    builds this image on first use, runs command (default: bash)
FROM docker.io/library/ubuntu:24.04

# oss-cad-suite release (YYYY-MM-DD): yosys, iverilog, verilator, sby + solvers,
# nextpnr-himbaechel (Gowin), openFPGALoader
ARG OSS_CAD_SUITE=2026-10-01

# g++/make: verilator compiles its models with the host C++ toolchain
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates curl git make g++ python3 python3-venv \
 && rm -rf /var/lib/apt/lists/*

RUN d=$(echo "$OSS_CAD_SUITE" | tr -d -) \
 && curl -fsSL "https://github.com/YosysHQ/oss-cad-suite-build/releases/download/$OSS_CAD_SUITE/oss-cad-suite-linux-x64-$d.tgz" \
  | tar -xz -C /opt

RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir cocotb==2.1.0 pytest==9.1.1

# venv first: oss-cad-suite ships its own python3
ENV PATH=/opt/venv/bin:/opt/oss-cad-suite/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1
