#!/usr/bin/env bash
# Build OTel-bootstrapped free5GC NF binaries from source, inside a golang container.
# Injects otelboot.go into each NF's cmd/ package, adds the OTel SDK + OTLP gRPC exporter
# deps (pinned to 1.24.0 to match free5GC's otel API), tidies, and builds a static binary.
# Replaces free5gc/util (server-side otelhttp) and free5gc/openapi (SBI mTLS client identity,
# CA verification, in-span httptrace; see openapi-patched/f5g_patch.go), and adds f5gtrace
# (goroutine-local trace parent, so SBI calls made while handling a request are its children).
set -euo pipefail
SRC="$(cd "$(dirname "$0")/../free5gc-src" && pwd)"
WORK="$(cd "$(dirname "$0")" && pwd)"
OUT="$WORK/bin"
NFS="${*:-nrf amf ausf udm udr}"
mkdir -p "$OUT"

docker run --rm \
  -v "$SRC":/src \
  -v "$WORK":/work \
  -v free5gc_gomod:/go/pkg/mod \
  -v free5gc_gocache:/root/.cache/go-build \
  -e NFS="$NFS" \
  -w /src golang:1.26 bash -euc '
    git config --global --add safe.directory "*" || true
    echo "=== prep patched util (server-side otelhttp) ==="
    cd /work/util-patched
    go get go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp@v0.49.0 >/dev/null
    go mod tidy >/dev/null 2>&1 || true
    for nf in $NFS; do
      echo "=================  building $nf  ================="
      d="/src/NFs/$nf"
      cp /work/otelboot.go "$d/cmd/otelboot.go"
      cd "$d"
      go mod edit -replace github.com/free5gc/util=/work/util-patched
      go mod edit -replace github.com/free5gc/openapi=/work/openapi-patched
      go mod edit -replace github.com/free5gc/f5gtrace=/work/f5gtrace
      go mod edit -require github.com/free5gc/f5gtrace@v0.0.0
      go get go.opentelemetry.io/otel/sdk@v1.24.0 \
             go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc@v1.24.0 >/dev/null
      go mod tidy >/dev/null 2>&1 || go mod tidy
      cd "$d/cmd"
      CGO_ENABLED=0 go build -o "/work/bin/$nf" . && echo "OK $nf -> bin/$nf"
    done
  '
echo "== built binaries =="; ls -la "$OUT"
