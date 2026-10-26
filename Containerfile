# openPIO development environment: lint, simulation, formal and FPGA tools.
# Same base as the GitHub Actions runners (ubuntu-24.04). Hardening (LibreLane)
# runs on GitHub Actions, see .github/workflows/gds.yaml.
#
#   scripts/dev.sh [command]    builds this image on first use, runs command (default: bash)
FROM docker.io/library/ubuntu:24.04

# oss-cad-suite release (YYYY-MM-DD): yosys, iverilog, verilator, sby + solvers,
# nextpnr-himbaechel (Gowin), openFPGALoader
ARG OSS_CAD_SUITE=2026-10-01

# g++/make: verilator compiles its models with the host C++ toolchain
# libpython3-dev: cocotb loads libpython into the simulator (test/Makefile)
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates curl git make g++ python3 python3-venv libpython3-dev \
 && rm -rf /var/lib/apt/lists/*

RUN d=$(echo "$OSS_CAD_SUITE" | tr -d -) \
 && curl -fsSL "https://github.com/YosysHQ/oss-cad-suite-build/releases/download/$OSS_CAD_SUITE/oss-cad-suite-linux-x64-$d.tgz" \
  | tar -xz -C /opt

# Icarus Verilog for cocotb (test/): the build Tiny Tapeout's gl_test job installs. The
# oss-cad-suite vvp wrapper forces its own PYTHONHOME, which breaks cocotb's embedded Python.
RUN curl -fsSLO https://github.com/TinyTapeout/iverilog/releases/download/v13.0/iverilog_13.0-1_amd64.deb \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ./iverilog_13.0-1_amd64.deb \
 && rm -rf iverilog_13.0-1_amd64.deb /var/lib/apt/lists/* \
 && rm /opt/oss-cad-suite/bin/iverilog /opt/oss-cad-suite/bin/vvp

RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir cocotb==2.0.1 pytest==8.4.2

# venv first: oss-cad-suite ships its own python3
ENV PATH=/opt/venv/bin:/opt/oss-cad-suite/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1
