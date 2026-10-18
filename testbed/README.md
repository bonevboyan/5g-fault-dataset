# Testbed

free5GC v4.2.3 on Kubernetes (k3d). The SBI runs over TLS with mTLS toward the NRF. There are
several gNBs, UEs that go idle and get paged, and the testbed pieces run on their own CPU sets.
Every run is a clean rebuild, and gates check readiness before any load is applied. Storage and
query sit outside the cluster and can move to another machine.

```
 k3d cluster "f5g" (one host, shared kernel; each node container pinned to its own cpuset)
 ┌ agent-0  role=core  cpus 0-5 ─────────────────┐ ┌ agent-1  role=ran  cpus 6-11 ──────────┐
 │ nrf amf ausf udm udr pcf nssf smf chf webui   │ │ gnb-0 … gnb-(G-1) pods                 │
 │  (native OTel, SBI https, NRF mTLS)           │ │  nr-gnb + K nr-ue processes per pod    │
 │ upf (gtp5g) + dn sidecar · mongo              │ │  N2 SCTP / N3 GTP-U over pod network   │
 └───────────────────────────────────────────────┘ └────────────────────────────────────────┘
 ┌ server-0  role=telemetry  cpus 12-15 ─────────────────────────────────────────────────────┐
 │ k3s control plane · otel-gateway (OTLP traces, K8s events, k8s_cluster, NF /metrics)      │
 │ otel-agent DaemonSet on every node (pod logs, kubelet cAdvisor) · node-exporter           │
 └──────────────────────────────────────────────┬────────────────────────────────────────────┘
                                                ▼ STORE_HOST (--store)
 storage tier (docker compose "f5g-store", cpus 16-19): Tempo :14317/:3200 · Loki :3100 ·
 Prometheus :9090 (remote-write) · Grafana :3300.  The workload driver also runs on cpus 16-19.
```

## Quick start

```bash
sudo insmod ../infra/gtp5g/gtp5g/gtp5g.ko         # once per boot (UPF data path)
./tb cluster-up                                   # k3d cluster, cpusets, image import
./tb store-up                                     # storage tier, pinned to cpus 16-19
./tb run --gnbs 3 --ues-per-gnb 50 --duration 600 --seed 7
./tb up --gnbs 3 --ues-per-gnb 50                 # bring-up only; then: ./tb ues | ./tb gates | ./tb down
```

`tb-compose` is the M1 single-host docker-compose testbed (one gNB, plain-HTTP SBI). It still
works and shares the workload driver.

## What a run is

1. **Clean rebuild.** `tb up` deletes and recreates the `f5g` and `f5g-telemetry` namespaces
   (fresh Mongo and NRF state), regenerates the NF configs from `config/`
   (`k8s/make_configs.py`), and loads the SBI PKI (`k8s/pki.sh`) as a Secret. The rendered
   manifests are saved to `runs/<id>/k8s/`.
2. **Staged, gated bring-up.** Each stage applies its own manifests and must pass its gate:

   | stage | brings up | gate |
   |---|---|---|
   | pre | — | gtp5g loaded · storage `/ready` · node roles Ready |
   | 0 | collectors, PKI Secret, config ConfigMap | otel-gateway Ready · **this run's logs + metrics are in Loki/Prometheus** |
   | 1 | MongoDB | readiness probe |
   | 2 | NRF | `SBI server started` (https, mTLS on) |
   | 3 | UPF (+ `dn` sidecar) | `UPF started` |
   | 4 | AMF AUSF UDM UDR PCF NSSF SMF CHF + webui (AF) | 8 NF types in `NfProfile` · SMF↔UPF PFCP association · all Ready · **traces in Tempo** |
   | 5 | — | G×K subscribers in UDR |
   | 6 | G gNB pods | each gNB `NG Setup procedure is successful` |
   | 7 | K UEs per gNB, started in parallel across gNBs | all registered · 2 PDU sessions each · each with a TUN data path |
   | ready | — | **zero pod restarts** during bring-up |

   Sessions that come up without a TUN (a UERANSIM name race) are released, and the UE
   re-establishes them; the count is recorded as `tun_repairs`. The zero-restart gate matters:
   a restarted NF has lost its state (see M1b-findings, SMF crash).
3. **Workload** (`../workload/driver.py`, 4 seeded activities on an absolute schedule; the same
   `--seed` perturbs the same UEs at the same offsets):
   - user-plane probes: pings from a rotating sample of 64 sessions to the DN host (the UPF pod
     at N6), at most 4 in flight per gNB, never overlapping a state snapshot → `kpi.jsonl`
   - registration churn: switch-off, then a full initial registration
   - PDU-session churn: release slice 2; the UE re-establishes it
   - **idle mode**: the gNB releases a UE to CM-IDLE; after a 5–30 s dwell the UE is woken either
     by uplink data (Service Request) or by downlink data from the DN (UPF buffering → SMF N1N2 →
     AMF paging → Service Request)
   - every attempt goes to `procedures.jsonl`, and population snapshots go to `population.jsonl`
   - after the workload, `../analysis/proc_timing.py` re-times each attempt from the UE/AMF/SMF
     log timestamps → `procedures_timed.jsonl`, with the log lines cached in `runs/<id>/logs/`
     (the driver's poll latency is only good to ~0.3 s)
4. **Manifest** `runs/<id>/manifest.json` records the gate latencies, pods (node, IP, image digest,
   restarts), node cpusets, config/PKI/manifest digest, TUN repairs, UE states at READY, and
   restarts during the run.

## Telemetry & join keys

| modality | source | labels |
|---|---|---|
| traces | free5GC native `otelhttp` client + server, W3C-propagated, incl. connection sub-spans | `run.id`, `service.name` |
| logs | every testbed pod (UE/NAS logs IMSI-prefixed, UE `[debug]` dropped at the source) | `run_id`, `service_name`, `k8s_*` |
| **K8s events** | `k8sobjects` watch (probe failures, restarts, scheduling) | `service_name="k8s-events"` |
| metrics | NF `/metrics` (`nf=`), kubelet cAdvisor (per pod), `k8s_cluster` (phase, restarts), node-exporter | `run_id` |
| KPIs / procedures | workload driver; log-timed by `proc_timing.py` | `ts_ns`, `ue`, `gnb` |

## Changes to free5GC (all in `../free5gc-otel/`, rebuilt by `build.sh`)

- **OTel SDK bootstrap** (`otelboot.go`) plus server-side `otelhttp` (`util-patched`). `/metrics`
  is not traced.
- **`openapi-patched`**: the SBI client presents its own certificate (upstream never does, so an
  NRF with OAuth+https rejects every NF), verifies servers against our CA (upstream sets
  `InsecureSkipVerify`), and creates httptrace sub-spans inside the client span (upstream made
  them orphan traces).
- **SMF race fix** (`patches/smf-default-path-cache-race.patch`): concurrent PDU session
  creations crashed the SMF with `concurrent map writes`.
- **End-to-end traces** (`f5gtrace`, `patches/e2e-trace-parent.patch`): a goroutine-local trace
  parent set by the gin middleware / AMF NGAP dispatcher / SMF PFCP dispatcher and used by the SBI
  client transport, so one NGAP message = one trace tree across NFs. `analysis/trace_shape.py`
  measures it.
- The webui is rebuilt the same way (it registers with the NRF as an AF).
