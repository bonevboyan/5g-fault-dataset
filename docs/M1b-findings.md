# M1b findings: Kubernetes, TLS, idle mode, multi-gNB, CPU isolation, observer overhead

Date: 2026-09-27. Host: i7-12700H (6P+8E cores, 20 threads), 31 GB RAM, kernel 7.0, snap Docker,
k3d v5.8.3 / k3s v1.31.5. free5GC v4.2.3 (patched, below) and UERANSIM v3.3.0. Code:
`testbed/` (`tb`, `k8s/`), `workload/`, `free5gc-otel/`, `analysis/ab_overhead.py`.

## What changed since M1

| item | M1 (compose) | M1b (k8s) |
|---|---|---|
| orchestration | docker compose, `restart: no` | k3d: 3 role-labelled nodes; Deployments with liveness/readiness probes; headless Services |
| CPU isolation | none (load generator saturated the host at 400 UEs) | node containers pinned: core 0-5 (P) · RAN 6-11 (P) · telemetry 12-15 (E) · storage + driver 16-19 (E) |
| SBI | plain HTTP/2 (h2c) | **https**. The NRF requires and verifies client certs (mTLS); clients verify servers against our CA |
| RAN | 1 gNB container | **G gNB pods** (default 3 × 50 UEs), UEs started in parallel |
| UE states | always CM-CONNECTED | **idle mode**: AN release → CM-IDLE → woken by uplink data (Service Request) or downlink data (**paging**) |
| modalities | traces, logs, metrics | + **K8s events** (`k8sobjects`), `k8s_cluster` object state, per-pod cAdvisor |
| UE logs | debug level | info (debug dropped at the source; `F5G_UE_DEBUG=1` keeps it) |
| trace quality | 88 % multi-NF after dropping orphan spans | **100 % multi-NF** (sample of 2000), 21 cross-NF edges, connection sub-spans kept |

## Validation (runs kept in `runs/`)

`m1b-final`: 3 gNBs × 50 UEs, 180 s, seed 7. The clean rebuild reached READY in **61 s**.

| metric | value |
|---|---|
| user-plane probes (unperturbed) | 3,823 · **0.00 % loss** · RTT p50 **0.88 ms** |
| + first probe of a session after an idle wake-up | 75 probes, 34 lost (user-plane re-activation, see below) |
| initial registration (switch-off + restart) | 18/18 · p50 632 ms |
| PDU release / UE re-establishment | 29/29 · 29/29 (4 T3580 recoveries) |
| AN release → CM-IDLE | 45/45 |
| Service Request, uplink-triggered | 17/17 · p50 440 ms |
| **Paging** → Service Request, downlink-triggered | 23/23 · p50 236 ms |
| pod restarts (bring-up / run) | 0 / 0 · TUN repairs 0 |
| free5GC `ERRO` lines | AMF 6 (the known PDU race), all others 0 |

Latencies are the driver's observations. Each state poll is a `kubectl exec` plus 2 `nr-cli` calls
at 4 Hz, so the resolution is about 0.25–0.4 s. *Update 2026-09-28:* `analysis/proc_timing.py`
re-times every procedure from UE/NF log timestamps; see "Log-timed procedures" below.

## Observer overhead (tracing on vs off; `analysis/ab_overhead.py`)

Same seed and workload, runs in ABBA order (on, off, off, on), n = 2 per arm:
`ab2-on-{1,2}` vs `ab2-off-{1,2}`.

- **Procedure latency: no measurable effect.** All 8 procedure medians agree within ±7 ms, well
  inside the bootstrap CIs. For example, registration is 587 vs 587 ms and paging-SR 217 vs 219 ms.
- **User-plane RTT: no effect.** 0.85 [0.83, 0.88] vs 0.86 [0.84, 0.88] ms.
- **NF CPU: +4.0 % of one core in total across the 9 NFs.** It's 19.3 (per run 21.6 / 16.9) vs
  15.3 (14.3 / 16.3), about +26 % relative, and the largest share is in AMF/SMF/UDR/NRF.
  UERANSIM is unchanged (≈52 %).
- The runs also show **schedule reproducibility**: every pair executed identical procedure
  counts on the same victims.

## Upstream bugs found and fixed (patches in `free5gc-otel/`)

1. **SMF crashes under concurrent PDU session creation** (`fatal error: concurrent map writes` in
   `GenerateDefaultPathToUPF`; the default-path cache is unsynchronised). k8s restarts the SMF, but
   the new process has lost every SM context. UEs keep "active" sessions with no network state,
   the SMF re-allocates IPs already in use (we saw duplicate UE IPs), and the data path fails
   silently. It shows up as soon as two gNBs attach UEs in parallel. Fix:
   `patches/smf-default-path-cache-race.patch` (a dedicated mutex on the path caches).
   *Worth reporting upstream.*
2. **The SBI client never presents a TLS client certificate** (`openapi` `NewHttp2Client`). With
   `https` + OAuth, the NRF requires and verifies client certs, so every NF → NRF call fails.
   Upstream also sets `InsecureSkipVerify` on the client. Fixed in `openapi-patched/f5g_patch.go`
   (client identity from `SBI_TLS_CERT/KEY`, server verification against `SBI_TLS_CA`).
   Hostname verification works because every NF is addressed by its short Service name (`nrf`,
   `amf`, …), which also matches the SAN `DNS:NRF` in the cert the NRF issues for itself.
3. **Orphan httptrace spans** (`openapi` builds the ClientTrace before the client span exists).
   The sub-spans are now created inside the span via `otelhttp.WithClientTrace`: 99.5 % of them
   have a parent, where upstream had 0 %.
4. **The metrics endpoint shares the traced server constructor**, so every probe and scrape
   started a trace. `util-patched` now excludes `/metrics`.

## Simulator (UERANSIM) limits and workarounds

- **Per-gNB-pod caps:** about 1,023 UEs (`Max UE count reached`) and 1,024 TUN devices, i.e. 512
  UEs × 2 sessions with a working data path. `tb` refuses layouts beyond that. More UEs means
  more gNB pods.
- **Concurrent `nr-ue` processes race for TUN names.** With UERANSIM's own policy routing, one
  lost race poisons that name for every later UE (stale route in `rt_uesimtunN`). We run
  `nr-ue -r` and probe with `ping -I <tun>`; a session left without a TUN is released and
  re-established (`tun_repairs`, 0 in all M1b runs).
- **IPv6 link-local chatter on a UE TUN counts as uplink data**, and woke every idle UE within
  0.3 s. IPv6 is disabled in the gNB pods.
- **UERANSIM re-establishes a configured PDU session by itself** after a release. The driver
  observes that; it never sends its own `ps-establish`.

## Measurement tooling effects found (and removed)

- **Ping bursts inflate RTT.** About 21 simultaneous echoes per gNB pod queue in the gNB's
  single user-space GTP loop, giving p50 ~12 ms. With at most 4 in flight it is **0.85 ms**.
- **State polling on the RAN node** (`nr-cli`) doubles to triples RTT when it overlaps a probe.
  Snapshots now run every 15 s and never overlap a probe round; per-procedure polling is 4 Hz.
- **The UE ceiling sweep (single gNB, compose):** the "collapse" at 400 UEs was our own tooling.
  With pings off, 400 UEs behaved like 50. The core itself handled 1,029 registered UEs /
  2,058 sessions at under 20 % CPU per NF.

## Baseline behaviour to document in the dataset schema (not faults)

- **PDU re-establishment race:** about 14–16 % of UE-initiated re-establishments recover only via
  T3580 (~16 s; `t3580_retry`). The AMF logs `SmContext … not found` / `Failed to update
  smContext`, which accounts for all remaining AMF `ERRO` lines.
- **User-plane re-activation after idle:** a Service Request re-activates only the session with
  pending data. The first packet on the UE's other session is lost while that session is
  re-activated (single-packet loss, the next probe succeeds). These probes are flagged
  `first_after_wake`.
- **Buffered downlink packets are dropped:** free5GC's UPF triggers paging but doesn't deliver
  the packets it buffered while the UE was idle.
- **Per-session WARN lines:** no BSF deployed; NSSF has no TA entry for TAC 1; CGF disabled;
  default NRF used for NSI.

## Testbed pitfalls found

- **Kubelet image GC and eviction on a shared laptop disk.** At over 85 % use, kubelet deleted
  our never-pullable images between runs, and all 4 A/B runs failed with `ErrImageNeverPull`. At
  under 10 % free it would evict pods mid-run. The cluster now sets image GC to 100/99 and
  eviction to under 2 %. `tb up` re-imports anything missing, and images go only to the node
  role that uses them (about 6 GB saved).
- **A restarted NF means lost state.** `tb up` now has a **zero-restarts gate**, and the manifest
  records restarts during the run.

## Not done / known limitations

- Mobility/handover is not modelled (UERANSIM limit). All gNBs share TAC 1, so paging goes to the
  whole TA.
- Nodes are logical (one host, shared kernel). The storage tier is on the same host but pinned
  to its own cores.
- There is no BSF or SCP, and no load-balanced NF replicas (one instance each).

## Log-timed procedures (added 2026-09-28)

`analysis/proc_timing.py runs/<id>` (run automatically by `tb run`, before teardown) takes each
driver record and finds the log lines that bound it: UERANSIM lines (ms, IMSI-prefixed) and
AMF/SMF lines (ns; AMF lines that carry only an AMF-UE-NGAP-ID are mapped to a SUPI). It writes
`workload/procedures_timed.jsonl` (the driver record plus a `log` object with start/end, latency,
the events used, and extras such as `t3580_retries`, `duplicate_psi`, `paging_to_sr_ms`). It
caches the lines it used in `runs/<id>/logs/`. The driver's poll latency is kept as the external
observer's view. All pods share one host clock; real multi-node hardware would need PTP/NTP.

`m1b-final`, p50 (poll → log):

| procedure | boundaries | poll | log |
|---|---|---|---|
| AN release | AMF gets UE Context Release Request → UE CM-IDLE | 1174 ms | **9 ms** |
| switch-off deregistration | UE MM-DEREGISTER-INITIATED → AMF removes the UE | 1124 ms | **40 ms** |
| initial registration | UE MM-REGISTER-INITIATED → Registration successful | 632 ms | **65 ms** |
| PDU re-establishment | request (UE, else AMF) → UE session up | 784 ms | **63 ms** (p90 16.4 s = T3580) |
| PDU release | SMF releases IP → AMF releases SmContext | 434 ms | **223 ms** |
| Service Request (uplink) | UE SR initiated → Service Accept | 440 ms | **9 ms** |
| paging → Service Request | AMF sends Paging → UE Service Accept | 236 ms | **27 ms** |

So most of the poll latency was observation lag: kubectl round-trips and the 4 Hz poll. The core
procedures take tens of ms. The two exceptions are real. First, the core-side PDU release takes
~220 ms: the SMF sends its SmContextStatusNotify to the AMF ~200 ms after the N2 release, whereas
the UE side (M1 runs, NAS debug on) finishes in ~19 ms. Second, the UE waits ~400 ms before it
re-requests a released session.

**The T3580 race, root cause (evidence from 14 runs):** in every run the T3580 retries coincide
(±1) with AMF `Duplicated PDU session ID` drops. The UE's new request arrives during that ~200 ms
window while the AMF still holds the old SmContext. The AMF then releases the old context locally
and discards the new request instead of forwarding it, so the UE's T3580 retransmission (~16 s
later) is what gets through. This is the free5GC AMF's duplicate-PSI handling, not UERANSIM.

The UE's NAS "Sending …" / "… received" debug lines are now kept (`ue.py`, a few lines per
procedure), so new runs time the UE side directly. Runs before 2026-09-28 fall back to
core-side events (see `EVENTS` in the script).

### Pitfalls found while validating this (2026-09-28)

- **Telemetry was silently lost after a host restart.** The collectors exported to
  `host.k3d.internal`, which k3d adds to CoreDNS's `NodeHosts`, and k3s rewrites `NodeHosts`
  when it restarts. Run `m1b-logtime` passed every gate and has no logs, metrics or traces.
  Now `tb` exports to the k3d network's gateway IP, and two new gates check that this run's
  logs + metrics (stage 0) and traces (stage 4) actually reach the storage tier.
- **mawk buffers its input.** A first `awk` version of the UE debug allowlist held quiet UEs'
  lines in the pipe until the process exited, so they were lost at teardown (`m1b-logtime2`,
  about 70 % of UE lines missing). The filter is now `sed -u`.
- **`nr-cli` has no reply timeout.** A state poll that raced a registration-churn restart
  blocked forever. It held the RAN-poll lock, so every later ping round waited (`m1b-logtime3`:
  1,010 pings instead of ~3,800). Every in-pod `nr-cli` call now runs under `timeout 5`.

`m1b-logtime{,2,3}` are kept only as evidence for these three failures, not as baselines.
- **Confound: host on battery.** `m1b-logtime3/4` show RTT p50 1.45–1.63 ms (baseline 0.87) and
  slower poll latencies. The laptop was discharging in the power-saver profile, so these runs are
  not performance baselines. Whether the `sed -u` UE filter costs anything is still open: re-check
  on AC power. Log-timed procedure coverage in `m1b-logtime4` is 100 % for all 8 procedures.


## End-to-end traces (added 2026-10-14)

Per-hop linkage was not enough: the microservice RCA datasets we compare against (RCAEval, Nezha,
DeepTraLog, AnoMod) publish one trace tree per request; only LO2 has single-span traces, and it
lists that as a limitation. free5GC gave one hop per trace because it never threads the incoming
request's `context.Context` into the SBI calls it makes while handling it (every consumer builds a
fresh context via `GetTokenCtx`).

Fix (`free5gc-otel/f5gtrace`, `patches/e2e-trace-parent.patch`): a goroutine-local "current trace
parent". The shared gin constructor (`util`) stores the request context for the handler's
goroutine; the shared SBI client transport (`openapi`) parents any outgoing request without a span
on it; the AMF NGAP dispatcher starts a root span per NGAP message (attributes `nas.msg`, `supi`,
`ngap.*_ue_ngap_id`), the SMF PFCP dispatcher per downlink-data Session Report (paging); the
fourteen `go` statements on the way to an SBI call (AMF callbacks, SMF PDU establishment and
status notify, UDM/UDR/PCF notifications) use `f5gtrace.Go`. Goroutine identity is parsed from
`runtime.Stack`, a few µs per lookup. The parent is carried as the span object, not its
SpanContext: otelhttp's transport takes its tracer from the parent span's provider, and a bare
SpanContext silently drops the client span (found in `e2e-1`, fixed in `e2e-2`).

`analysis/trace_shape.py runs/<id>` (Tempo) on 1,500 sampled traces, 3 gNBs × 50 UEs, 180 s:

| | `m1b-final` (per hop) | `e2e-2` (end to end) |
|---|---|---|
| spans / trace p50 · p90 · max | 5 · 5 · 5 | 6 · 86 · 126 |
| services / trace p50 · p90 · max | 2 · 2 · 2 | 2 · 8 · 8 |
| tree depth p50 · p90 · max | 1 · 1 · 1 | 2 · 8 · 8 |
| traces with ≥ 3 services | 0 % | 34 % |
| root is an SBI client span (no procedure) | 100 % | 1.9 % (NRF heartbeats) |

Per root: registration request (`InitialUEMessage / RegReq`) 31 spans · 5 services · depth 6
(AMF → NRF, AUSF → UDM → UDR); PDU session establishment (`ULNASTransport`) 86 spans · 8 services ·
depth 8 (AMF → SMF → UDM, PCF → UDR, CHF, NRF, and the N1N2 transfer back to the AMF); paging
(`pfcp Session Report Request`, DLDR only) 6 spans, SMF → AMF.

Limits. (1) A 5G procedure is several NAS messages with the UE in the loop, so it is one tree per
NGAP message, not per procedure; procedures are joined by `supi` + the log-timed window from
`proc_timing.py`. (2) Work handed to a plain `go` statement we did not wrap, or triggered by a
timer, still starts a new trace. (3) The two runs are not performance baselines (laptop on battery).

**Regression noticed while validating (not caused by this change):** since the duplicate-PSI
patch and per-gNB TACs of 2026-09-28, ~25–35 % of AN releases, registrations and PDU releases time
out in the driver (`fix-psi-tac2`, `e2e-1`, `e2e-2`; 0 % in `m1b-final`). The SMF logs ten
`Send SMContext Status Notification Problem … 500` from the AMF (the patched path) and the AMF
aborts paging after 4 × T3513 for 7 UEs (tracking areas). Bisect pending.
