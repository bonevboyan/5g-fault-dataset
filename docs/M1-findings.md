# M1 findings: stable testbed

Date: 2026-09-27. Host: kernel 7.0.0-31, snap Docker, gtp5g loaded. free5GC v4.2.3. All 9
control-plane NFs run on native-OTel images, and the CHF was added in M1. UERANSIM v3.3.0.
Code: `testbed/` (see its README) and `workload/`.

## M1 success criteria and status

| criterion | status | evidence |
|---|---|---|
| readiness gates | ✅ | 7-stage gated bring-up in `tb up`; each gate's latency goes into the manifest |
| N-UE workload | ✅ 50 UEs | `workload/driver.py`: pings, registration churn, PDU churn, procedure outcomes |
| clean rebuild per run | ✅ | `down -v` + fresh cert copy, so no carried state. Clean start → READY with 50 UEs in **25 s** |
| off-host storage tier | ✅ (separable; tested on the same host) | `testbed/storage/` is its own compose project; the collector only needs `--store <host>` |

## Validation runs (kept in `runs/`)

`m1-repro-c` and `m1-repro-d`: 50 UEs, 180 s, `--seed 7`, run back to back as separate clean
rebuilds.

| metric | repro-c | repro-d |
|---|---|---|
| pings through the UPF / loss | 5441 / 0.02 % (1 ping) | 5449 / 0.00 % |
| RTT p50 (UE → UPF → DN gateway) | 1.47 ms | 1.18 ms |
| initial registration (switch-off + restart), ok / p50 | 17/17 · 143 ms | 17/17 · 142 ms |
| PDU sessions after registration, ok / p50 | 17/17 · 391 ms | 17/17 · 384 ms |
| PDU release, ok / p50 | 31/31 · 118 ms | 31/31 · 114 ms |
| PDU re-establishment, ok (1 aborted at end of run) · T3580 retries | 30/30 · 5 | 30/30 · 5 |
| churn victim sequence | identical | identical |
| multi-NF traces | — | 93 % (19 distinct cross-NF edges, incl. `smf→chf`) |

Across these runs, free5GC logged 0 `ERRO` lines except the AMF race described below. Per-NF
CPU stayed under 1 %, and UERANSIM used about 8 % CPU with 50 UEs, so the UE ceiling on this
host is well above 50 (not yet measured).

## Problems found and fixed

1. **M0's "AMF can not select a target AMF by NRF"** was a subscription problem, not a
   GUAMI/TAI one. The UE requests slices `1/010203` and `1/112233`, but the subscriber only
   subscribed the first. `InSubscribedNssai` therefore failed, and the AMF went into slice
   re-routing (NSSF, then looking for a target AMF). `config/subscriber.json` now subscribes
   both slices in AM, SM, SMF-selection and SM-policy data.
2. **Registration race** (M0): solved by staged bring-up with gates. For example, all 8 NF
   types are confirmed in `NfProfile` before the gNB starts.
3. **The NRF rewrites `cert/nrf.pem` at start-up**, so a read-only cert mount crashes it. Each
   run now gets a fresh writable copy (`testbed/.state/cert`), which also keeps the repo's
   certs untouched.
4. **Snap Docker** keeps its data under `/var/snap/docker/common/var-lib-docker`, and `/` isn't
   a shared mount. `tb` now detects `DockerRootDir`, and node-exporter drops `rslave`.
5. **Duplicate MSISDN**: the webconsole returns 409 for a second subscriber with the same GPSI.
   Provisioning now assigns `msisdn-09<i>` per UE.
6. **The CHF was missing**, which caused SMF `no CHF found` errors on every session. After
   adding it, **CHF deadlocked under concurrent charging requests**. The requests went through
   the CGF/CDR FTP export path, and PDU sessions failed. Setting `cgf.enable: false` in
   `config/chfcfg.yaml` fixes it; charging data requests still succeed (201). CDR export to
   the billing domain is out of scope for the dataset.
7. **Trace noise**: `free5gc/openapi@v1.3.0 client.go:275` calls
   `otelhttptrace.NewClientTrace(ctx)` before the `otelhttp` client span exists. Its
   connection sub-spans (`http.getconn/dns/connect/send/headers`) then form orphan traces,
   about 3 per SBI call, which was 77 % of all traces. The collector drops that
   instrumentation scope. *Follow-up:* patch `openapi` so the ClientTrace is built inside the
   span. That would recover the connection-timing spans, which could be useful for
   network-fault RCA.
8. **UERANSIM `-n N` can't churn a single UE.** A normal de-registration leaves the UE in
   `ATTEMPTING-REGISTRATION` behind a back-off timer, and switch-off doesn't stop that UE's
   thread. So each UE now runs as its own `nr-ue` process, and churn is switch-off followed by
   a restart (a full initial registration).
9. **UERANSIM re-establishes a configured PDU session by itself** about 60–400 ms after it's
   released. An extra `ps-establish` collided with it: UAC "barred", a T3580 retry, and a
   duplicate PSI. The driver now only observes the UE's own re-establishment.

## Baseline behaviour to keep in mind for M2+ (not fixed; it's the core under test)

- **free5GC PDU re-establishment race.** About 16 % of re-establishments (5/31 in both runs)
  hit it. When the UE's automatic re-establishment arrives while the AMF is still tearing
  down the old SmContext for the same PSI, the AMF logs
  `SmContext[PDU Session ID:2] not found` and the request goes unanswered. The UE recovers on
  T3580, about 16 s later. `procedures.jsonl` flags these with `t3580_retry: true`. It is
  normal fault-free behaviour under this workload, so AD baselines will see it in "normal"
  data. It must be documented in the dataset schema, not labelled as an injected fault.
- **Remaining single-service traces** (~7 %) are standalone server-side `sbi` spans. Their
  callers don't propagate context, so they aren't artifacts.
- **Traces are linked per hop, not as one end-to-end tree** (unchanged from M0).

## Reproducibility

- Churn slots come from per-activity RNGs seeded with `(seed, activity)`, on an absolute time
  grid. Each slot runs in its own thread, so a T3580 stall can't shift later slots. With the
  same seed, the UE → procedure sequence was identical across runs. Latencies and T3580
  occurrences come from the core, and naturally vary.
- `runs/<id>/manifest.json` records: gate latencies, image IDs, a config SHA-256 over
  `config/`, `collect/`, `cert/` and the compose file, container IDs/IPs (to map logs to
  containers), host kernel/CPU, UE states at READY, and the workload timestamps.

## Not done / next

- The storage tier has only been exercised on the same host. Moving it to another machine is
  a matter of `--store`, but that hasn't been tested.
- The UE-count ceiling hasn't been measured (50 was comfortable). Try 100/200 before M3
  sizing.
- No Grafana dashboards yet; only datasources are provisioned.
- The k3d path (PLAN decision 2) was not pursued in M1. The compose testbed is tier-separated
  instead, because free5GC + gtp5g + UERANSIM work reliably there. Porting to k8s/VMs means
  re-hosting tier 1; tier 2 and the collector contract stay the same.
