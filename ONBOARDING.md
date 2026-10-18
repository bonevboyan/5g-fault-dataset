# Onboarding: 5G core fault-injection dataset

Written 2026-10-18 for whoever joins the project. It says what exists, which decisions were
made and why, how to get the testbed running on your own machine, and what the next piece of
work is. The milestone documents in `docs/` hold the evidence behind every claim here; this
file is the map.

## 1. What the project is

We are building a public dataset for anomaly detection (AD) and root-cause analysis (RCA) on a
cloud-native 5G core. The gap it fills: no public dataset combines logs + metrics + real
distributed traces (with parent/child spans) + Kubernetes events + UE-plane KPIs with RCA
ground-truth labels for a 5G core. Generic microservice RCA datasets (Nezha, Eadro, RCAEval,
DeepTraLog, LO2, AnoMod) never use a 5G core; 5G datasets (5GDatasets, 5GCID) are pcap/IDS
oriented. It is the successor of Boyan's BSc thesis ("Cross-Layer Fault Atlas"), fixing its
four limitations: no distributed traces, single node with co-located observers, light workload,
one run per fault.

The deliverable is: a FAIR dataset release (Zenodo/4TU), a scripted replication package (this
repo), preliminary AD/RCA baselines, and a paper. Full design: `docs/PLAN.md`.

Milestones: M0 tracing de-risk (done), M1 stable testbed (done), M1b realism upgrade (done),
M2 fault library (next), M3 batch collection, M4 analysis + release.

## 2. Repository map

| path | what |
|---|---|
| `docs/PLAN.md` | design, locked decisions, roadmap, open questions |
| `docs/M0-findings.md`, `M1-findings.md`, `M1b-findings.md` | what was tried, measured, fixed, per milestone |
| `docs/5g_fault_catalogue.pdf` | the fault taxonomy (levels A–E) the library will implement |
| `docs/m0-compose-overlay/` | historical M0 files that sat on top of upstream free5gc-compose |
| `testbed/` | **the testbed**: `tb` (k3d control CLI), `k8s/` (manifests, config rendering, PKI), `config/` (NF/gNB/UE configs), `storage/` (Tempo/Loki/Prometheus/Grafana compose), `collect/` (collector config), `tb-compose` + `compose.core.yaml` (older M1 docker-compose testbed, still works) |
| `workload/` | `driver.py` (seeded N-UE workload), `ue.py` (UERANSIM control shared with `tb`) |
| `analysis/` | `proc_timing.py` (log-timed procedure latencies), `trace_shape.py` (trace tree stats from Tempo), `ab_overhead.py` (tracing on/off A/B) |
| `free5gc-otel/` | everything that changes free5GC: `otelboot.go`, `f5gtrace/`, `openapi-patched/`, `util-patched/` (submodule + diff), `patches/`, `build.sh`, `apply-patches.sh`, `Dockerfile.<nf>` |
| `free5gc-src/` | submodule: upstream free5GC at the commit we build from (NFs are its own submodules) |
| `infra/gtp5g/gtp5g/` | submodule: the gtp5g kernel module (UPF data path), built for kernel 7.0 |
| `infra/k3d/`, `deploy/`, `scripts/` | M0-era k3d/Multus/OTel-operator experiments; superseded by `testbed/k8s` |
| `runs/` | every validation run referenced in the docs (manifests, workload records, cached logs) |
| `collector/`, `faults/` | empty placeholders for M2/M3 |

Not in the repo: built NF binaries (`free5gc-otel/bin/`, rebuilt by `build.sh`), generated PKI
(`testbed/k8s/pki/`), per-run state (`testbed/.state/`), the k3d images, the thesis PDF and the
reference papers (ask Boyan).

## 3. Decisions and why

1. **free5GC, not Open5GS.** free5GC already has `otelhttp` at every SBI client and server call
   site; it only lacks an SDK bootstrap. Open5GS may be added later as a second system for the
   cross-implementation claim, budget permitting.
2. **Native OpenTelemetry, not eBPF.** Beyla fails the BPF verifier on kernel 7.0, and the Go
   auto-instrumentation only sees the server side because free5GC's SBI client is an h2c
   `http2.Transport`, invisible to the `net/http` probes. So no `traceparent` was ever injected
   and every span was its own trace (the exact wall the thesis hit). Activating free5GC's own
   instrumentation is kernel-independent and deterministic. (M0)
3. **Rebuild the NF binaries, swap them into the stock images.** `otelboot.go` is dropped into
   each NF's `cmd/`, and `free5gc/util` and `free5gc/openapi` are replaced via `go mod replace`
   with our patched copies. Images are `free5gc/<nf>:otel2`; `otel2-pre-e2e` tags are the
   rollback before end-to-end traces.
4. **End-to-end traces via a goroutine-local trace parent (`f5gtrace`)**, not by threading
   `context.Context` through every NF. free5GC builds a fresh context for every outgoing call,
   so per-hop links never chained into one tree. Threading ctx would touch hundreds of
   signatures; the goroutine-local parent is a few hundred lines in shared code plus small
   per-NF hunks (NGAP and PFCP root spans, 14 `go` statements wrapped). Result: one tree per
   NGAP message, depth up to 8, 8 services. The parent is carried as the span object, not its
   SpanContext (a bare SpanContext silently drops the client span). (M1b, "End-to-end traces")
5. **k3d logical multi-node on one laptop, with cpusets.** Core NFs on P-cores 0-5, RAN
   (UERANSIM) on 6-11, telemetry on E-cores 12-15, storage tier + workload driver on 16-19.
   This came after the compose testbed showed the load generator saturating the host and
   polluting measurements. Real VM nodes are a later step; the manifests should not change.
6. **Two-tier pipeline.** Collectors in the cluster (`f5g-telemetry` namespace) export to a
   storage tier (`testbed/storage`, docker compose project `f5g-store`) that outlives rebuilds
   and can move off host via `--store`. Runs are separated by `run_id`, not by wiping storage.
7. **Every run is a clean rebuild with gated staged bring-up.** M0 showed NFs registering with
   the NRF before Mongo/NRF were ready, and stale NRF state after image swaps. `tb up` deletes
   and recreates the namespaces, then brings up 8 stages, each with a gate (collectors → Mongo
   → NRF → UPF → control-plane NFs → subscribers → gNBs → UEs). A zero-pod-restarts gate exists
   because a restarted NF has lost its state (see the SMF crash below).
8. **SBI over https with NRF mTLS.** Realism for TLS faults in the catalogue (level D). This
   exposed an upstream bug (the SBI client never presents a client cert), which we patched.
9. **One `nr-ue` process per UE.** A shared `nr-ue -n N` cannot re-register one UE (deregistration
   parks it behind a back-off timer). Registration churn is therefore switch-off + fresh process.
10. **The driver never issues `ps-establish`.** UERANSIM re-establishes a released session by
    itself ~60–400 ms later; a driver-issued one collided with it (UAC barred, duplicate PSI).
11. **Workload schedule is seeded on an absolute time grid.** Per-activity RNGs seeded with
    `(seed, activity)` choose offsets and victim UEs, each slot in its own thread. Same seed
    gives the same victims at the same offsets, verified across runs. This is what makes
    fault runs comparable with fault-free ones.
12. **Procedure latencies are re-timed from logs**, not from the driver's 4 Hz state polling.
    The driver's numbers carried 0.25–0.4 s of observation lag; `proc_timing.py` uses UE/AMF/SMF
    log timestamps and shows the real core-side latencies are tens of ms. Driver numbers are
    kept as "the external observer's view".
13. **Measurement artefacts were removed rather than tolerated:** at most 4 pings in flight per
    gNB pod (the gNB's single GTP loop queues bursts), state snapshots never overlap probes,
    IPv6 disabled in gNB pods (link-local chatter woke idle UEs), UE debug logs dropped at the
    source, `/metrics` excluded from tracing.
14. **CHF runs with `cgf.enable: false`.** With CDR/FTP export on, the CHF deadlocked under
    concurrent charging requests. Billing export is out of scope.
15. **Images are imported per node role with `imagePullPolicy: Never`, and kubelet image GC /
    eviction thresholds are overridden** in `testbed/k8s/cluster.yaml`. The laptop disk sits at
    ~85–95 %, and kubelet deleted our images between runs and tainted nodes with
    `disk-pressure`. `tb up` re-imports whatever is missing.
16. **Upstream code is pinned as git submodules; our changes are diffs.** `free5gc-src`,
    `free5gc-otel/util-patched` and `infra/gtp5g/gtp5g` point at the upstream commits we built
    from. `free5gc-otel/patches/applied/*.diff` are exact snapshots of the modified working
    tree and are what `apply-patches.sh` applies; `patches/*.patch` are the same changes split
    by topic for reading. Note that the end-to-end patch's AMF `callback.go` hunks are
    cumulative over the duplicate-PSI patch, so the topic patches do not apply in sequence;
    use `apply-patches.sh`.
17. **Faults are phased.** v1 = levels E (Chaos Mesh baseline, kept identical to generic
    microservice datasets for comparability), A/B (signalling and PFCP/GTP-U protocol faults),
    D (NRF/Mongo/TLS dependency faults). Level C (container security) is v2 and gated. All on an
    isolated testbed with synthetic subscribers.

## 4. What was done, per milestone

**M0 (2026-09-27): real parent/child traces from free5GC.** Proved Beyla and Go
auto-instrumentation cannot do it on this stack, found the h2c root cause, activated native
OTel with `otelboot.go` + server-side `otelhttp` in `util`. Jaeger showed cross-NF
client → server spans for every SBI hop of a registration. `docs/M0-findings.md`.

**M1 (2026-09-27): stable compose testbed.** `tb-compose`: gated bring-up, 50 UEs, clean
rebuild in 25 s, separable storage tier, seeded workload with reproducible victim sequences.
Fixed: subscriber slice subscription (the real cause of "AMF can not select a target AMF"),
NRF rewriting its cert file, snap Docker paths, duplicate MSISDN on provisioning, missing CHF
and the CHF deadlock, orphan httptrace spans. `docs/M1-findings.md`.

**M1b (2026-09-27 → 2026-10-14): realism.** Moved to k3d with cpusets; https + NRF mTLS; 3 gNBs
× 50 UEs; idle mode with uplink Service Request and downlink paging; K8s events modality;
observer-overhead A/B (no latency effect, +4 % of one core across 9 NFs); log-timed procedures;
end-to-end traces. Validation run `m1b-final`: 0 % ping loss, RTT p50 0.88 ms, every
procedure 100 % success, 0 restarts. `docs/M1b-findings.md`.

**Upstream free5GC bugs found and patched** (all in `free5gc-otel/`):
- SMF `concurrent map writes` crash in `GenerateDefaultPathToUPF` under parallel PDU session
  creation; after the restart every SM context is gone and IPs get re-allocated. Worth reporting
  upstream. `patches/smf-default-path-cache-race.patch`.
- SBI client never presents a TLS client certificate and sets `InsecureSkipVerify`.
  `openapi-patched/f5g_patch.go`.
- httptrace sub-spans created before the client span exist (orphan traces). Same file.
- AMF drops a PDU session request whose PSI duplicates a context still being released, so
  the UE recovers only via T3580 (~16 s). `patches/amf-duplicate-psi-reprocess.patch`. **This
  patch is under suspicion, see section 5.**

**Baseline behaviours that are not faults** and must be documented in the dataset schema:
the T3580 re-establishment race (~14–16 % of UE re-establishments without the PSI patch), the
single lost first packet on the other session after an idle wake-up (`first_after_wake`),
dropped buffered downlink packets during paging, and a few per-session WARN lines (no BSF, no
NSSF TA entry, CGF disabled).

## 5. Where it stands now and what to do next

**Open problem (blocking M2).** Since the changes of 2026-09-28, about 25–35 % of AN releases,
registrations and PDU releases time out in the driver (runs `fix-psi-tac2`, `e2e-1`, `e2e-2`)
versus 0 % in `m1b-final`. Two things changed that day:
1. the AMF duplicate-PSI patch (`patches/amf-duplicate-psi-reprocess.patch`);
2. one TAC per gNB instead of a shared TAC 1 (`k8s/make_configs.py --tacs`, called from `tb`
   with `--tacs <gnbs>`).

Evidence: the SMF logs `Send SMContext Status Notification Problem … 500` from the AMF (the
patched path), and the AMF aborts paging after 4 × T3513 for ~7 UEs (tracking areas). The PSI
patch also did not remove the T3580 retries (5 in `e2e-1`, 0 in `e2e-2`).

Plan: bisect with two runs on AC power, 3 gNBs × 50 UEs, 180 s, seed 7:
- (a) AMF with the PSI patch reverted (`git apply -R` the PSI hunks in `free5gc-src/NFs/amf`,
  rebuild the AMF with `build.sh amf`, rebuild and re-tag the `free5gc/amf:otel2` image);
- (b) `--tacs 1` (edit the call in `tb`, or make it a flag).
Compare failure rates in `runs/<id>/workload/procedures.jsonl` and
`procedures_timed.jsonl`. Then revert one or both, and either keep the T3580 race as documented
baseline behaviour or fix it properly. M2 must start from a 0 % baseline or every fault run
inherits this noise.

**Then M2: the fault library.** Config-as-data fault definitions (`faults/`), one per catalogue
entry for levels E, A/B and D; a label schema (fault class, target NF, injection start/stop,
magnitude, expected first-affected layer/NF); temporal boundaries aligned to the workload's
absolute schedule. Tooling candidates named in the plan: Chaos Mesh (E), UERANSIM at scale and
a scripted gNB (A), pfcpsim and scapy (B).

Other open questions from `PLAN.md`: UE-count ceiling on this hardware (the core handled 1,029
UEs on compose; per gNB pod UERANSIM caps at ~1,023 UEs / 512 UEs with 2 sessions), runs per
fault versus wall-clock budget, whether to add Open5GS, moving the storage tier to a second
machine (untested, `--store`), Grafana dashboards (none yet).

## 6. Setting it up from a fresh clone

Host used so far: i7-12700H (20 threads), 31 GB RAM, Ubuntu with kernel 7.0.0-31, snap Docker,
k3d 5.8.3 (k3s 1.31.5), kubectl, helm, gcc/make for the kernel module. `go` is not needed on the
host; builds run in a `golang:1.26` container. The cpuset layout in `testbed/tb` assumes 20
CPUs (`CPUSETS`, `STORE_CPUS`); adapt it for another machine.

```bash
git clone --recursive git@github.com:bonevboyan/5g-fault-dataset.git
cd 5g-fault-dataset

# 1. Kernel module for the UPF data path (needs root; redo after every reboot, rebuild after a kernel upgrade)
(cd infra/gtp5g/gtp5g && make)
sudo insmod infra/gtp5g/gtp5g/gtp5g.ko          # or: scripts/01-load-gtp5g.sh

# 2. Apply our source changes to the pinned upstream checkouts, build the NF binaries
free5gc-otel/apply-patches.sh
free5gc-otel/build.sh nrf amf ausf udm udr pcf nssf smf chf     # -> free5gc-otel/bin/<nf>

# 3. Images: stock free5GC images with our binary swapped in (tag otel2 is what `tb` imports)
cd free5gc-otel
for nf in nrf amf ausf udm udr pcf nssf smf chf; do docker build -t free5gc/$nf:otel2 -f Dockerfile.$nf .; done
cd ..
docker pull free5gc/upf:v4.2.3 free5gc/ueransim:latest mongo:4.4   # pulled by name; see NODE_IMAGES in tb

# 4. Cluster, storage tier, PKI, run
cd testbed
./tb cluster-up            # k3d cluster "f5g", pins node cpusets, imports images per node role
./tb store-up              # Tempo :14317/:3200, Loki :3100, Prometheus :9090, Grafana :3300 (admin/admin)
./tb run --gnbs 3 --ues-per-gnb 50 --duration 180 --seed 7
```

`tb up` generates the test PKI (`k8s/pki.sh`) and renders configs on every run. The webui
(`free5gc/webui:otel2`, registers with the NRF as an AF) was built by hand the same way:
`otelboot.go` placed in `free5gc-src/webconsole/`, `replace github.com/free5gc/util =>
/work/util-patched` in its `go.mod`, `go build` in the container, then `Dockerfile.webui`.
`build.sh` does not script it yet. If you only need the core up, `tb up` and `tb down` exist;
`tb gates` re-checks every gate on a live stack, `tb ues` lists UE state.

Run output lands in `runs/<id>/`: `manifest.json` (gate latencies, pods with node/IP/image
digest/restarts, cpusets, config digest, UE states at READY), `k8s/` (rendered manifests),
`workload/{kpi,procedures,procedures_timed,population}.jsonl`, `logs/*.jsonl.gz` (the log
lines `proc_timing.py` used). Traces/logs/metrics live in the storage tier under `run_id`.

### Host quirks that cost time before

- `sudo` prompts for a password, so `tb` cannot load gtp5g for you; `tb`'s pre-gate checks it.
- Snap Docker: data under `/var/snap/docker/common/var-lib-docker`, `/` is not a shared mount,
  bind-mounting `/tmp` paths fails (pipe scripts in with `docker run -i … < file`).
- Disk pressure: below the kubelet's 2 % eviction threshold it deletes our imported images and
  taints every node `disk-pressure`; the taint clears only ~5 min after pressure ends, so a run
  right after freeing space still fails at stage 0. Check `df -h /` before long runs.
- After a host restart k3s drops `host.k3d.internal` from CoreDNS; `tb` now exports telemetry
  to the k3d network gateway IP and gates on telemetry actually arriving.
- The Loki container has been found exited after a reboot; `tb store-up` before a run.
- Runs on battery are not performance baselines (`e2e-1`/`e2e-2` were on battery).

## 7. Reading order

1. `docs/PLAN.md` (15 min). 2. `testbed/README.md` for the run model and the telemetry join
keys. 3. `docs/M1b-findings.md` end to end; it has the current numbers and all known
limitations. 4. `docs/M0-findings.md` and `docs/M1-findings.md` when you need the history of a
decision. 5. `free5gc-otel/f5gtrace/f5gtrace.go` and `openapi-patched/f5g_patch.go` headers
before touching tracing.
