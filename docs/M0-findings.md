# M0 findings — how to get real distributed traces (with parent_id) from free5GC

Date: 2026-09-27. Testbed: free5gc-compose (free5GC **v4.2.3**, Go 1.26.2), Jaeger, host kernel 7.0.

## What we proved empirically

1. **free5gc-compose runs cleanly** on this host; all core NFs come up, register with the NRF over
   the SBI, gNB (UERANSIM) completes NG Setup, and a UE registration fires the real SBI chain
   (`AMF → AUSF /nausf-auth/v1/ue-authentications → …`). (Auth 404s only because no subscriber
   was provisioned yet — the cross-NF SBI calls still happen and are what we trace.)

2. **Beyla (eBPF) does NOT work on kernel 7.0.** Both Beyla 1.9.6 and 2.6.4 fail the BPF verifier
   on their gRPC probe (`uprobe_ClientConn_Invoke_return: invalid read from stack`), which is marked
   required and aborts every NF tracer. Same "new kernel breaks eBPF tooling" class as older gtp5g.

3. **OpenTelemetry Go auto-instrumentation (the tool the k8s OTel Operator injects) DOES load on
   kernel 7.0** and attaches `net/http` client+server probes to all NFs. But it captures only the
   **server** side. free5GC produced isolated single-span traces per NF — **no parent_id linkage**.

4. **Root cause of the missing linkage:** free5GC's SBI client (`free5gc/openapi` `NewHttp2Client`)
   uses **`golang.org/x/net/http2.Transport` with `AllowHTTP=true` (h2c prior-knowledge)**, not
   `net/http.Transport`. The eBPF `net/http` probe never sees the client call, so no `traceparent`
   is injected → every server span starts a fresh trace. This is the same wall the thesis hit
   (Beyla single-spans, no parent_ids).

5. **But free5GC already has native OTel instrumentation at every SBI call site**: the client wraps
   its h2c transport with `otelhttp.NewTransport(...)` + `otelhttptrace.NewClientTrace`, and the
   server uses the same stack. The instrumentation points exist in the code.

6. **It is dormant, though.** Neither v4.2.3 nor `main` compiles in the OTel **SDK** or an OTLP
   exporter — the otel deps are all `// indirect`, there is no TracerProvider/exporter/propagator
   bootstrap, and no telemetry config in any NF cfg. So `otelhttp` uses the global **no-op** provider
   and **no-op** propagator: no spans exported, no `traceparent` injected.

## Conclusion — the tracing strategy for the dataset

**Do not use eBPF.** Activate free5GC's own native OpenTelemetry:

- Add a small **OTel SDK bootstrap** to free5GC — OTLP exporter (→ collector/Tempo/Jaeger),
  a `sdktrace.TracerProvider`, and the **W3C `tracecontext` + baggage propagator** — set as the
  process globals (`otel.SetTracerProvider`, `otel.SetTextMapPropagator`) early in each NF's startup.
  Best placed in the shared `free5gc/util` init that every NF already calls, so one change lights up
  all NFs; gate it on `OTEL_EXPORTER_OTLP_ENDPOINT` so it's a no-op when unset.
- **Rebuild** the NF images via `free5gc-compose/docker-compose-build.yaml` (build from source).
- Because `otelhttp` is already wired on both client and server, this bootstrap yields **authentic,
  W3C-propagated distributed traces with real parent/child spans across NFs** — kernel-independent
  (no eBPF), deterministic, and reproducible. Strictly better than the thesis's eBPF single-spans.

This supersedes the earlier plan of eBPF auto-instrumentation via the OTel Operator (kept only as a
fallback). The k8s pipeline (M1+) will carry the same source-bootstrapped images.

## Assets produced this milestone

- `free5gc-compose/` — cloned, core NFs + gNB running.
- `free5gc-compose/docker-compose-tracing.yaml` — Jaeger + (Beyla, now shown unusable on k7.0).
- `free5gc-compose/otel-instrument.sh` — per-NF eBPF sidecars (works to load, but can't stitch h2c;
  kept as evidence / fallback).
- `infra/gtp5g/gtp5g/gtp5g.ko` — built for kernel 7.0.
- k3d cluster `fault5g` (3 nodes) with cert-manager, Tempo, OTel Operator installed (for M1).

## OUTCOME — real parent_id traces achieved (2026-09-27)

Implemented and verified. Two source changes, both in shared code so one edit covers all NFs:
- **Client + SDK:** `free5gc-otel/otelboot.go` — an `init()` dropped into each NF's `cmd/` package
  that installs a global `TracerProvider` (OTLP/gRPC exporter) + W3C `tracecontext` propagator,
  gated on `OTEL_EXPORTER_OTLP_ENDPOINT`. Activates free5GC's already-present client `otelhttp`.
- **Server:** patched `free5gc/util` `httpwrapper.NewHttp2Server` to wrap the SBI handler with
  `otelhttp.NewHandler(...)` (`free5gc-otel/util-patched`), so each NF **extracts** the incoming
  `traceparent` and emits a server span. Wired into builds via a `go mod replace`.

Built patched binaries in a `golang:1.26` container (`free5gc-otel/build.sh`), swapped them into
`free5gc/<nf>:otel` images (`free5gc-otel/Dockerfile.<nf>`), ran via
`docker-compose-otel.yaml`. After a UE registration, Jaeger shows **cross-NF parent/child spans**:

```
[amf] HTTP PUT  (client)
   └─ [nrf] sbi (server)      <-- child, linked by W3C traceparent across NFs
```

36 multi-service traces captured. **This is the parent_id-linked distributed tracing the thesis
never had** (its eBPF/Beyla traces were single-span). Kernel-independent, deterministic. Milestone
M0 met.

## Verified deeper (2026-09-27, after provisioning subscriber `imsi-208930000000001`)

With the subscriber provisioned, **authentication succeeds** and every SBI hop of the registration
chain is captured as a linked client→server pair (parent_id):

```
amf -> ausf: 18    ausf -> udm: 18    udm -> udr: 22
amf -> nrf: 60     ausf -> nrf: 21    udm -> nrf: 5    (+ amf -> udm, NSSF discovery)
```

Two honest limits found:
1. **Per-hop vs end-to-end tree.** Links are per SBI call; they don't chain into one request tree
   (`AUSF→UDM` starts a new trace) because free5GC doesn't thread the *incoming* request
   `context.Context` into its *outgoing* calls. A single end-to-end trace per procedure would need
   threading ctx through every NF's call graph — a large source change (optional stretch). *Update 2026-10-14: done, see M1b-findings "End-to-end traces"; the per-hop claim below was too
   generous, the microservice datasets do have full trees.* Per-hop
   linkage is what the thesis wholly lacked.
2. **Full registration completion** (Registration Accept + PDU session) needs a *clean* stack:
   hot-swapping `:otel` images into a running stack left stale AMF/NSSF registrations in the NRF
   (`"can not select a target AMF by NRF"`) and an NSSF TAC-config warning. These are M1
   stable-testbed concerns (scripted clean bring-up + aligned config), not tracing issues.

## Clean-stack attempt (2026-09-27) — completion is an M1 config task

Built all 8 control-plane NFs on `:otel` (nrf, amf, ausf, udm, udr, pcf, nssf, smf) and scripted a
clean bring-up (`scripts/up-clean.sh`, `scripts/provision-subscriber.sh`, `subscriber.json`).
Two ordering/config issues surfaced that a proper readiness-gated M1 startup must handle:
1. **Registration race:** NFs registered with the NRF before Mongo/NRF were ready, so only AMF
   landed in `NfProfile`; a restart of the other NFs fixed it (all 7 types then present). → M1 needs
   readiness gates (NRF+Mongo up, all NFs registered) before injecting load, like the thesis did.
2. **AMF selection:** registration then fails at `"AMF can not select a target AMF by NRF"` — a
   GUAMI/TAI config-alignment issue in free5GC's AMF-selection flow (not a tracing problem). Needs
   aligned `servedGuamiList`/`supportTaiList`/NSSF TAC config; deferred to M1.

None of this affects the tracing result: the parent_id linkage across all registration SBI hops was
already captured and verified above. Full registration-accept + PDU-session (adds `AMF→SMF`,
user-plane) is gated only on the M1 config alignment, not on instrumentation.

## Remaining for a full-depth trace (next)

Current multi-service traces are 2 spans (NF→NRF) because the UE registration fails at auth (no
subscriber provisioned) — `AMF→AUSF` returns 404 before AUSF calls UDM. To get the full
`AMF→AUSF→UDM→UDR` (+ `AMF→SMF→UPF` PFCP) chain, provision the subscriber (webconsole API,
`imsi-208930000000001`) for a *successful* registration + PDU session, then confirm the deep chain
links (validates that free5GC threads `context.Context` through its call graph). Then extend the
build to all NFs and carry the `:otel` images into the k8s pipeline (M1).
