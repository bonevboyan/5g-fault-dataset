#!/usr/bin/env bash
# Load the gtp5g kernel module (free5GC UPF dependency) into the HOST kernel.
# Needs root. k3d agent containers share the host kernel, so loading it once
# here makes it available to the UPF pod. Re-run after a kernel upgrade (rebuild first).
set -euo pipefail
KO="$(cd "$(dirname "$0")/../infra/gtp5g/gtp5g" && pwd)/gtp5g.ko"

if lsmod | grep -q '^gtp5g'; then
  echo "gtp5g already loaded:"; lsmod | grep '^gtp5g'; exit 0
fi
if [[ ! -f "$KO" ]]; then
  echo "ERROR: $KO not found. Build it first:"
  echo "  cd infra/gtp5g/gtp5g && make"
  exit 1
fi
echo "Loading $KO ..."
sudo insmod "$KO"
lsmod | grep '^gtp5g' && echo "OK: gtp5g loaded."
