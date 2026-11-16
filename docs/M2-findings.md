# M2 findings: fault library

Started 2026-11-15. Scope (PLAN.md, "M2 scope decisions"): levels E and D, 10 runs per fault.

## Tooling

- **Recipes** in `faults/<level>/<id>.yaml`, format and label schema in `faults/README.md`.
- **`tb run --fault <recipe>`** injects the recipe on a timer relative to the workload start
  (`testbed/faults.py`, a background thread next to the workload driver) and writes
  `runs/<id>/labels.json` (schema `f5g-labels/1`). Every run writes it; fault-free runs have
  `"fault": null`.
- **Chaos Mesh 2.8.8** for level E, installed by `tb chaos-up` (`testbed/k8s/chaos-mesh-values.yaml`):
  controller on the telemetry node, daemons on the core and RAN nodes only, images imported by
  `tb` like every other image (the k3d nodes have no registry access).
- **`analysis/fault_signals.py runs/<id>`** checks that a fault is visible in every modality at
  the labelled time: per 10 s bin around the injection, ERRO/WARN log lines, K8s events on the
  target, target CPU and memory, error traces, failed UE procedures and ping loss.

## First fault: `E-pod-kill-smf` (run `m2-podkill-smf-1`)

The SMF pod is killed 60 s into a 3 gNB × 50 UE, 180 s, seed-7 workload. Labels: injected
60.2 s after the workload start; the new SMF pod was Ready and re-associated with the UPF over
PFCP (`recovery_log`) 1.7 s later.

| 10 s bin vs injection | −60 … −10 | **0** | +10 | +20 … +50 |
|---|---|---|---|---|
| ERRO+WARN log lines | 10–25 | **459** | 831 | 20–23 |
| K8s events on smf | 0 | **18** | 0 | 0 |
| smf memory (MiB) | 40–44 | **3.4** | 26 | 26–36 |
| traces with error spans | 0–1 | **125** | 31 | 2–6 |
| failed UE procedures | 0 | **2** | 1 | 0–3 |
| ping loss | 0 % | **100 %** | 92 % | 10–14 % |

Every modality shows the fault in the labelled bin. SMF CPU is too small to show it (0.01–0.07
cores), so memory is the metric that carries it.

**The service does not fully recover when the SMF does.** 33 of the 259 PDU sessions that were
still pinged after +30 s never answered again: the new SMF has no context for them, while the UEs
still consider them active. The data path stays broken until the UE re-establishes the session,
which this workload only does for the sessions it happens to churn. So `recovered_ns` means "the
target NF works again", not "the service is healthy again". The residual damage is part of the
fault's signature and is visible in the UE records.

## All level-E recipes, one validation run each (2026-11-15)

Same workload for every run (3 gNBs × 50 UEs, 180 s, seed 7), onset at 60 s, held 60 s
(partition 30 s, kills one-shot). Runs `m2-e-<recipe>-1`; per-run tables in
`runs/<id>/fault_signals.txt`. Baseline for comparison: procedure latency p50 ~500 ms, ping RTT
~0.4 ms, 0 failures, 10–30 ERRO/WARN lines per 10 s.

| recipe | where it shows | strength | notes |
|---|---|---|---|
| `E-pod-kill-smf` | all modalities | strong | 13 % of sessions never recover (see above) |
| `E-pod-kill-amf` | memory reset, K8s events, errors, failed procedures (5–8 / 10 s) | strong, **permanent** | the UERANSIM gNBs never redo NG Setup, so nothing new is served for the rest of the run (`recovered_ns: null`); established data paths keep working |
| `E-pod-kill-upf` | ping loss 100 %, error traces, failed procedures | strong, **permanent** | GTP-U tunnels and PFCP sessions are gone; the data path does not come back |
| `E-pod-kill-nrf` | memory reset, K8s events | metrics + events only | NFs keep using cached peers; no UE impact |
| `E-pod-kill-udm` | memory reset, K8s events, a few error traces | metrics + events only | no UE impact in this workload |
| `E-cpu-stress-amf` | AMF CPU 0.01 → 3.4 cores | metrics only | no latency or failure impact: the AMF needs very little CPU |
| `E-mem-stress-smf` | SMF memory 40 MiB → 1 GiB | metrics only | no limits, so no OOM kill |
| `E-net-delay-smf` (200 ms) | procedure latency ×4–10 (~500 → 2000–4800 ms) | strong in latency | few failures |
| `E-net-loss-amf` (20 %) | procedure latency ~×1.6 (~500 → ~850 ms) | subtle | SCTP/HTTP2 retransmit; netem rule verified in the AMF netns (`loss 20%`) |
| `E-net-partition-smf-upf` (30 s) | ERRO/WARN ×60 (1800 / 10 s), failed procedures | strong | |
| `E-net-delay-upf` (50 ms, all UPF egress) | RTT 0.4 → 50 ms, **and** 3–9 failed procedures / 10 s | strong, cross-plane | slower PFCP replies trigger the SMF/AMF state races; a user-plane fault spilling into the control plane |
| `E-net-delay-n3` (50 ms, UPF → gNBs only) | RTT 0.4 → 50 ms | pure user plane | no control-plane effect at all |

Tooling fixes found on the way: Chaos Mesh clears its injection records when a fault ends, so
`faults.py` now reads them right after injection (a missing record is logged); the checker gained
procedure-latency and ping-RTT rows, because network faults show up as slowness, not failures.

**Decision (2026-11-15): the two permanent faults are kept as they are and documented.** After
an AMF or UPF kill the rest of the run is a full outage, because the simulated RAN does not do
what real gNBs do (re-establish NGAP after an AMF restart), and nothing re-creates the lost GTP-U
tunnels after a UPF restart. `recovered_ns` stays `null` for the AMF kill. Dataset users must know
that after these two faults, "no recovery" is a property of the simulated RAN, not of the core.

## Level-D recipes, one validation run each (2026-11-15)

Level D breaks what the core depends on: its database, its NRF registry and its certificates.
Same workload as level E (3 gNBs × 50 UEs, 180 s, seed 7), onset 60 s, held 60 s, then reverted.
Runs `m2-d-<recipe>-1`; tables in `runs/<id>/fault_signals.txt` (`--span 120`, so they include
the minute after the revert).

Tooling: a second injector, `steps` (`apply` and `revert` actions; `faults/README.md`), with
two actions: `mongo` (JavaScript run in the testbed MongoDB, which also stores the NRF registry)
and `cert` (restart the target with a bad key/cert via an optional per-NF secret,
`f5g-pki-override-<nf>`, `testbed/k8s/render.py`). Database network faults use Chaos Mesh on
`app=db`. Manual checks done before writing the recipes: free5GC's NRF reads the registry from
MongoDB on every discovery (no cache), the AMF discovers the SMF afresh for every new session, and
an NF removed from the registry never re-registers by itself.

| recipe | failed procedures while active (of all) | where it shows | notes |
|---|---|---|---|
| `D-db-unreachable` | 15 of 58: session setup, registration, AN release | failed procedures, error traces | **quiet**: hardly any extra ERRO/WARN lines; the UDR/PCF/NRF just wait on MongoDB |
| `D-db-slow` (100 ms) | 0 of 70 | procedure latency p50 ~500 → ~840 ms | subtle, latency only |
| `D-db-auth-corrupt` | 5 of 64: all initial registrations | failed registrations, AMF "MAC failure" WARN | the UEs that registered during the fault stay deregistered (UERANSIM does not retry after an Authentication Reject): 150 → 145 registered |
| `D-db-sm-data-missing` | 13 of 68: session setup on slice 2 | failed sessions, SMF ERRO (`Get SessionManagementSubscriptionData error: 500`), error traces | slice 1 unaffected; cause in the UDR, symptom at the SMF |
| `D-nrf-poison-smf` | 22 of 59: session setup, AN release, registration | failed procedures, 10–20 s latencies (connect timeouts), error traces | the SMF itself logs nothing; the AMF does |
| `D-nrf-drop-udm` | 4 of 62: all initial registrations | failed registrations, AUSF ERRO (`UDM discovery failed`) | the UDM logs nothing; 3 UEs stay deregistered after the revert |
| `D-tls-expired-smf` | 16 of 34: session setup/release, paging, AN release | everything: ERRO ×100 (`tls: expired certificate`), SMF memory reset, events, ping loss 100 % | the SMF cannot register with the NRF and exits after ~20 s; active sessions 300 → 70, back to 296 a minute after the revert |
| `D-tls-untrusted-udm` | 14 of 62: session setup, registration | ERRO ×4, error traces, UDM memory reset, events | |

What level D adds over level E: in five of the eight recipes, the component that is broken
(the database or the NRF) shows nothing in its own logs or metrics. The evidence is entirely in
other NFs and in the UE records. These are the cases a root-cause method has to trace back across
the service graph, which is the point of the level.

**Known behaviour, kept:** a UE whose registration fails during `D-db-auth-corrupt` or
`D-nrf-drop-udm` stays deregistered for the rest of the run, because UERANSIM does not retry. As
with the AMF/UPF kills in level E, this is a property of the simulated RAN.

## Level-A recipes (signalling storms), one validation run each (2026-11-16)

Level A floods the control plane with extra UERANSIM UEs instead of breaking a component. A new
`storm` injector (`testbed/faults.py`) starts a burst of extra UEs across the gNB pods at a high
IMSI index (base 900000) the workload's UEs never reach, so the storm never collides with the
legitimate UEs; the effect is read from the AMF/SMF and from the legitimate UEs' latency and ping.
Three recipes, same workload as E/D (3 gNBs × 50 UEs, 180 s, seed 7), onset 60 s, held 60 s, then
reverted. Runs `m2-a-<recipe>-1`.

| recipe | what it floods | where it shows | effect on legitimate UEs |
|---|---|---|---|
| `A-reg-storm-authfail` (300 UEs, no subscriber) | registration + authentication, every attempt rejected at the UDM | ERRO/WARN 20 → **1827**, held the full 60 s; error traces 0 → **301**, held | none: ping loss 0 %, latency flat |
| `A-reg-storm-valid` (200 UEs, provisioned) | full registration + PDU session setup, succeeds | ERRO/WARN 20 → **1231** in one 10 s bin (the burst), then back to baseline; AMF memory 25 → **159 MiB**, settling to ~50; almost no error traces | none: ping loss 0 %, latency flat |
| `A-paging-storm` (50 UEs, paged in a loop) | paging / service-request path (AMF paging, SMF N1N2MessageTransfer, UPF buffering) | adds the storm UEs' paging procedures (initial_registration +18, an_release +15, service_request_mo +10, service_request_mt_paging +4) and a one-bin ERRO/WARN burst to ~330 | none: ping loss 0 %, latency flat |

The three have distinct signatures: the **auth-reject** storm is a *sustained* flood of
reject/error logs and error traces (the core keeps processing and failing registrations for the
whole 60 s); the **valid** storm is a *brief* signalling burst plus a lasting memory step (the
extra 200 subscribers' retained registration/session state), with no errors because every
registration succeeds; the **paging** storm is the only one that drives the paging and
service-request path, visible as extra paging/service-request procedures.

**Finding, reported as-is: the storms are loud but benign on this hardware.** In all three the AMF
has enough headroom that its CPU barely moves and the legitimate UEs see 0 % ping loss and
unchanged latency. The faults are clearly detectable (logs, traces, procedure counts, the memory
step) but do not degrade service. Storms that would actually starve the core need more generated
load than one laptop can drive; that waits for more capacity (Gunship), the same reason the
packet-level faults (A's GTP-U half, level B) are deferred. The paging storm is also the weakest
signal of the three: its driver releases and pages each UE over a per-UE `kubectl exec`, so it
completes only about one pass over the 50 UEs in 60 s — a burst rather than a heavy flood.

**Scope:** level A here is the registration/paging slice of catalogue A1/A3. Its signalling-flood
character is captured by these three; heavier floods and the packet-level storms are future work.

## Level-B recipes (PFCP / N4 floods), one validation run each (2026-11-16)

Level B abuses the control protocol between the SMF and the UPF, PFCP on N4 (UDP/8805). A new
`pfcp_flood` injector (`testbed/faults.py` + `testbed/pfcp_flood.py`) blasts the UPF's N4 port with
hand-built PFCP messages, as a rogue or overloading SMF would. Delivery note: no NF/gNB pod has
python or nc, but the gNB pods have bash, so the packets (built stdlib-only) are sent from inside
the cluster with bash's `/dev/udp`, spread over every gNB pod. The flood is **rate-limited**
(`rate` packets/s per pod, default 2000 ≈ the bash send ceiling): an unpaced loop pegs a host CPU
and skews the whole run. Same workload as the other levels (3 gNBs × 50 UEs, 180 s, seed 7), onset
60 s, held 60 s, reverted. Runs `m2-b-pfcp-<recipe>-1`. User decision (2026-11-16): a hand-rolled
flooder, not `pfcpsim` (which targets a different UPF and may not interoperate with free5GC).

Encoding note worth recording: free5GC's UPF (go-pfcp) **rejects an IPv4 Node ID** in an
Association Setup Request with "unexpected EOF", but **accepts an FQDN Node ID**. The association
flood therefore sends a fresh FQDN (`rogueN.flood`) per packet, and each one registers a new peer.

| recipe | what it sends | where it shows | effect on legitimate UEs |
|---|---|---|---|
| `B-pfcp-heartbeat-flood` | well-formed PFCP Heartbeat Requests | **metrics only**: UPF CPU 0.01 → ~0.15 cores; ERRO/WARN unchanged (heartbeats are handled silently) | none |
| `B-pfcp-assoc-flood` | Association Setup, fresh FQDN Node ID each (new peer every time) | UPF CPU 0 → ~0.23 cores, ERRO/WARN 20 → ~400 (association churn), UPF memory 11 → 15 MiB | slight: brief ping-loss blips (≤1.6 %), latency within noise |
| `B-pfcp-malformed-flood` | valid header, truncated/garbage body | **huge log flood**: ERRO/WARN 20 → **~54 000** per 10 s ("unexpected EOF"), sustained; UPF CPU ~0.10 cores | none |

The three sit in three different modalities: the heartbeat flood is visible **only in metrics**
(a stealthy flood — nothing in the error logs), the malformed flood is an **enormous error-log**
signal with little else, and the association flood sits in between with a bit of everything plus
real association state at the UPF.

**Finding, reported as-is: like level A, the N4 floods are detectable but mostly benign on this
hardware at a controlled rate.** The UPF has enough headroom that CPU rises only to a fraction of a
core and legitimate UEs are essentially unaffected. An earlier *unpaced* run did drive the UPF CPU
to ~1.7 cores and spiked legitimate UEs' latency, but it also pegged a host CPU and skewed the
run's own clock (a 20-minute gap before injection, a 50-minute teardown), so that magnitude is an
artifact of host starvation, not a clean measurement. A flood heavy enough to actually starve this
UPF cannot be driven cleanly from one laptop; a high-rate, user-impacting N4 flood waits for more
capacity (Gunship), together with level B's user-plane (GTP-U) half.

**Scope:** level B here is the PFCP control-message slice. The GTP-U/user-plane abuse and a
Chaos-Mesh N4 net-degradation baseline for contrast are future work.
