#!/usr/bin/env bash
# Attach OpenTelemetry Go eBPF auto-instrumentation to each free5GC NF as a sidecar
# sharing that NF's PID + network namespace. This is the same tool the k8s OTel
# Operator injects, and it does W3C traceparent propagation over the HTTP/2 SBI,
# yielding real parent/child spans across NFs. Works on kernel 7.0 (Beyla does not).
set -euo pipefail
NFS=("${@:-nrf amf ausf udm udr smf pcf nssf}")
NFS=(${NFS[@]})
IMG=otel/autoinstrumentation-go:latest

for nf in "${NFS[@]}"; do
  docker rm -f "otel-$nf" >/dev/null 2>&1 || true
  docker run -d --name "otel-$nf" \
    --privileged \
    --pid="container:$nf" \
    --network="container:$nf" \
    -v /sys/kernel/debug:/sys/kernel/debug \
    -e OTEL_GO_AUTO_TARGET_EXE="/free5gc/$nf" \
    -e OTEL_EXPORTER_OTLP_ENDPOINT="http://jaeger:4317" \
    -e OTEL_EXPORTER_OTLP_PROTOCOL=grpc \
    -e OTEL_SERVICE_NAME="$nf" \
    -e OTEL_TRACES_SAMPLER=always_on \
    "$IMG" >/dev/null
  echo "attached otel-$nf -> /free5gc/$nf"
done
