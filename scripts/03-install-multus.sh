#!/usr/bin/env bash
# Install Multus CNI (thick plugin) on the k3d/k3s cluster.
# free5GC's towards5gs charts need Multus for the N2/N3/N4/N6 secondary interfaces.
# k3s keeps its CNI dirs in non-default locations, so we patch the daemonset to match.
set -euo pipefail

MULTUS_URL="https://raw.githubusercontent.com/k8snetworkplumbingwg/multus-cni/master/deployments/multus-daemonset-thick.yml"

kubectl apply -f "$MULTUS_URL"

# Point Multus at k3s' CNI bin/conf dirs (default manifest assumes /opt/cni/bin & /etc/cni/net.d).
kubectl -n kube-system patch daemonset kube-multus-ds --type=json -p='[
  {"op":"replace","path":"/spec/template/spec/containers/0/args",
   "value":["--multus-conf-file=auto","--multus-autoconfig-dir=/host/etc/cni/net.d","--cni-conf-dir=/host/etc/cni/net.d","--cni-bin-dir=/host/opt/cni/bin"]}
]' 2>/dev/null || echo "(patch skipped or already applied)"

echo "== waiting for Multus daemonset =="
kubectl -n kube-system rollout status daemonset/kube-multus-ds --timeout=180s
kubectl -n kube-system get pods -l app=multus -o wide
