# 5G Core Fault-Injection Dataset — Design & Roadmap

A multimodal **anomaly-detection / root-cause-analysis dataset for a cloud-native 5G core**.
The paper's contribution *is the dataset*, in the mold of LO2 and AnoMod but 5G-specific and
with real distributed traces. Successor to the BSc "Cross-Layer Fault Atlas" thesis, fixing its
four stated limitations.

## Novelty (the gap)

No public dataset combines **logs + metrics + true distributed traces (with `parent_id`) +
K8s events + UE-plane KPIs** with **RCA ground-truth labels** for a **cloud-native 5G core**.
- Generic-microservice AD/RCA datasets (Nezha, Eadro, DéjàVu, RCAEval, DeepTraLog, LO2, AnoMod)
  never use a 5G core.
- Existing 5G datasets (5GDatasets, 5GCID) are pcap/IDS-oriented — no traces, no RCA labels.
- The thesis is closest but has no distributed traces, was single-node with co-located observers,
  light workload, one run per fault, and shipped an atlas + raw telemetry (not an AD/RCA dataset).

## Locked decisions (2026-09-27)

| # | Decision | Choice |
|---|----------|--------|
| 1 | 5G core + tracing | **free5GC primary.** Tracing via free5GC's **native OTel** (`otelhttp` is already wired at every SBI call site) activated by a small OTel **SDK bootstrap** + rebuild — NOT eBPF. eBPF auto-instrumentation can't stitch free5GC's h2c client and Beyla fails on kernel 7.0. See [M0-findings](M0-findings.md). Open5GS possible later as a 2nd system. |
| 2 | Testbed topology | **k3d logical multi-node (in use since M1b)**, with role-labelled nodes pinned to disjoint cpusets (core / RAN / telemetry) and storage outside the cluster. VM-based multi-node later. Two-tier pipeline, so observers can move off-host without redesign. |
| 3 | Fault scope | **Comprehensive but phased.** v1 = Levels E + A/B + D. v2 = Level C (adds a security/audit modality; gated). |
| 4 | Primary uses | **All four** (RCA, cross-layer diagnosis, cross-modal AD, cross-system generalization) — served by one rich label schema, not extra injection work. |

## Fault taxonomy (from [`5g_fault_catalogue.pdf`](5g_fault_catalogue.pdf))

- **E — operational baseline** (Chaos Mesh): CPU, memory, pod-kill, net delay, packet loss, dependency. *Kept identical to generic-microservice datasets for cross-system comparability.*
- **A — control-plane signaling**: registration storms, NAS/auth floods, NGAP floods. *(UERANSIM at scale + scripted gNB.)*
- **B — PFCP / user-plane protocol**: PFCP assoc/malformed floods (pfcpsim), GTP-U abuse (scapy). Plus one Chaos-Mesh net-degradation baseline for contrast.
- **D — dependency & data-layer**: NRF poisoning, MongoDB tamper, SBI/SCP TLS failure.
- **C — container/orchestration security** *(v2, gated)*: privileged-container / rogue-sidecar / RBAC-token proxies observed via Falco/eBPF (new modality).

All faults framed as observability characterization on an isolated local testbed with synthetic
subscribers (thesis Responsible-Research posture): no real targets, standard public tooling.

## Modalities & schema

logs (Loki) · metrics (Prometheus: cAdvisor, node-exporter, kube-state-metrics, core exporters) ·
**distributed traces w/ parent_id (Tempo/Jaeger via OTel)** · K8s events · UE RTT / user-plane KPIs ·
5G-native "procedure outcomes" (NAS/NGAP results, PDU-session success).
**Shared join key:** `trace_id` + timestamps across every modality (LO2's lesson).
**Per-run labels (RCA ground truth):** fault class, target NF, injection start/stop, magnitude,
expected first-affected layer/NF. **Many repetitions per fault** (≥10) for confidence intervals.

Release: FAIR on Zenodo/4TU + scripted replication package + preliminary AD & RCA baselines.

## Roadmap

- **M0 — de-risk tracing (done).** Native-OTel free5GC gives parent/child SBI spans. See [M0-findings](M0-findings.md).
- **M1 — stable testbed (done 2026-09-27).** Readiness gates, N-UE workload, clean rebuild per run,
  separable storage tier. 50 UEs are stable and schedules reproduce. See [M1-findings](M1-findings.md).
- **M1b — realism upgrade (done 2026-09-27; end-to-end traces added 2026-10-14).** The testbed moved to Kubernetes (k3d), and CPU sets
  now separate core / RAN / telemetry / storage. Also added: SBI TLS with NRF mTLS, multiple
  gNBs, idle mode with Service Request and paging, a K8s-events modality, UE logs at info level,
  and a tracing-overhead A/B. Two upstream free5GC bugs were fixed on the way (SMF crash under
  concurrent PDU sessions; SBI client never presents a TLS cert). See [M1b-findings](M1b-findings.md).
- **M2 — fault library.** Config-as-data "Anomaly Library" (E + A/B + D), labeling, temporal boundaries.
- **M3 — batch + collection.** ≥10 runs/fault, per-phase multimodal capture, join-key wiring.
- **M4 — analysis + release.** AD/RCA baselines, schema docs, Zenodo/4TU, paper.

## Environment (verified 2026-09-27)

- Host: 20 threads, 31 GB RAM, kernel **7.0.0-31-generic**, KVM available.
- Tooling present: docker, kubectl 1.35, helm 3.20, kind 0.27, k3d 5.8.3, gcc/flex/bison, make.
- **gtp5g builds cleanly against kernel 7.0** (`infra/gtp5g/gtp5g/gtp5g.ko`). Load needs root (see scripts).
- Absent (install later for true multi-node): k3s, multipass/libvirt, `go`.

## Open questions still to resolve

- ~~Does free5GC stitch `parent_id`s across the SBI?~~ Yes, per hop, with the native-OTel bootstrap (M0).
- ~~free5GC deployment path~~ For now: our own tier-separated compose testbed (`testbed/`), with
  k8s/VM re-hosting of tier 1 later.
- **Regression since 2026-09-28** (duplicate-PSI patch + per-gNB TACs): ~30 % procedure timeouts
  vs 0 % in `m1b-final`; SMF gets 500s on status notify, AMF aborts paging. Bisect, then revert
  one or both (M1b-findings, "End-to-end traces").
- The free5GC PDU re-establishment race (~16 % T3580 recoveries in fault-free runs). Root cause
  found 2026-09-28: the AMF drops a request whose PSI duplicates a context that is still being
  released (M1b-findings, "Log-timed procedures"). Either patch it or document it as baseline
  behaviour.
- ~~Procedure timing from polling~~ Log-timed since 2026-09-28 (`analysis/proc_timing.py`).
- Whether to add Open5GS as a 2nd system (cross-implementation claim) given one-laptop budget.
- UE count ceiling on this hardware; runs-per-fault vs. wall-clock budget.
