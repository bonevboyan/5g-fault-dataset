#!/usr/bin/env bash
# Clean, scripted bring-up of the OTel-instrumented free5GC core + Jaeger,
# provision the subscriber, register a UE, and establish a PDU session.
# Resolves the stale-NRF-state problems from hot-swapping images into a live stack.
set -euo pipefail
cd "$(dirname "$0")/.."
BASE=docker-compose.yaml
OTEL=docker-compose-otel.yaml

echo "### tearing down (wipe volumes for clean NRF/Mongo state)"
docker compose -f "$BASE" -f "$OTEL" down -v --remove-orphans 2>/dev/null || true
docker rm -f jaeger beyla 2>/dev/null || true

echo "### starting core NFs (instrumented ones on :otel) — also creates the network"
docker compose -f "$BASE" -f "$OTEL" up -d \
  db free5gc-nrf free5gc-amf free5gc-ausf free5gc-udm free5gc-udr \
  free5gc-pcf free5gc-nssf free5gc-smf free5gc-upf free5gc-webui

echo "### starting Jaeger on the core network"
docker run -d --name jaeger --network free5gc-compose_privnet \
  -e COLLECTOR_OTLP_ENABLED=true -p 16686:16686 -p 4317:4317 -p 4318:4318 \
  jaegertracing/all-in-one:1.62.0 >/dev/null

echo "### waiting for webconsole"
until curl -sf -o /dev/null http://localhost:5000/api/login -X POST \
  -H 'Content-Type: application/json' -d '{"username":"admin","password":"free5gc"}'; do sleep 2; done
sleep 5   # let NFs register with NRF

echo "### provisioning subscriber"
bash scripts/provision-subscriber.sh

echo "### starting gNB + UE"
docker compose -f "$BASE" up -d ueransim
sleep 6
docker exec ueransim pkill -f nr-ue 2>/dev/null || true
docker exec -d ueransim ./nr-ue -c ./config/uecfg.yaml
sleep 15

echo "### UE status"
docker exec ueransim ./nr-cli imsi-208930000000001 -e status 2>/dev/null | grep -iE 'mm-state|rm-state|5u-state' || true
docker exec ueransim sh -c 'ip -o addr show | grep uesimtun || echo "(no PDU tunnel yet)"'
