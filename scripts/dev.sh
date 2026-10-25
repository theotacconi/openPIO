#!/usr/bin/env bash
# Run a command in the openPIO dev container (see Containerfile), nothing installed on the host.
#
#   scripts/dev.sh                                   interactive shell
#   scripts/dev.sh python3 test/lockstep/fuzz.py     one command, from the current directory
#
# The image is built on first use and rebuilt whenever Containerfile changes (tag = its hash).
# Only the repository is mounted, at the same path; files created inside belong to the caller.
#
# Env overrides:
#   ENGINE   container engine   (podman, else docker)
set -euo pipefail
root=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)

ENGINE=${ENGINE:-$(command -v podman || command -v docker || true)}
[ -n "$ENGINE" ] || { echo "need podman or docker" >&2; exit 1; }

tag=openpio-dev:$(sha256sum "$root/Containerfile" | cut -c1-12)
if ! "$ENGINE" image inspect "$tag" >/dev/null 2>&1; then
    echo "dev.sh: building $tag (first use or Containerfile changed)" >&2
    "$ENGINE" build -t "$tag" -f "$root/Containerfile" "$root/scripts"   # context: no repo files needed
fi

cwd=$PWD
[[ $cwd == "$root" || $cwd == "$root"/* ]] || cwd=$root
# :z (shared SELinux label), not :Z: a private label would lock out other running containers
args=(run --rm -i -v "$root:$root:z" -w "$cwd" -e HOME=/tmp)
[ -t 0 ] && [ -t 1 ] && args+=(-t)
if [[ $(basename "$ENGINE") == podman ]]; then
    args+=(--userns=keep-id)
else
    args+=(--user "$(id -u):$(id -g)")
fi
[ $# -gt 0 ] || set -- bash
exec "$ENGINE" "${args[@]}" "$tag" "$@"
