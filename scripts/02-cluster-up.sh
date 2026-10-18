#!/usr/bin/env bash
# Bring up (or recreate) the k3d multi-node cluster and show node roles.
set -euo pipefail
CFG="$(cd "$(dirname "$0")/../infra/k3d" && pwd)/cluster.yaml"
NAME=fault5g

if k3d cluster list 2>/dev/null | grep -q "^${NAME} "; then
  echo "Cluster '${NAME}' exists. Delete + recreate for a clean state? [y/N]"
  read -r ans; [[ "${ans:-N}" =~ ^[Yy]$ ]] && k3d cluster delete "$NAME" || { echo "Keeping existing."; exit 0; }
fi

k3d cluster create --config "$CFG"
kubectl wait --for=condition=Ready nodes --all --timeout=180s
echo "== nodes + roles =="
kubectl get nodes -L role -o wide
