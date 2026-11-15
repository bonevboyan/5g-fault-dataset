# Fault library

One YAML file per fault ("recipe"). `tb run --fault faults/<level>/<name>.yaml` injects it during
the workload and writes the ground-truth labels to `runs/<id>/labels.json`. A run without
`--fault` writes the same file with `"fault": null`, so fault-free runs are labelled as normal.

Levels follow `docs/5g_fault_catalogue.pdf`. v1 (decided 2026-11-15): **E** (operational
baseline, Chaos Mesh) and **D** (dependency and data layer).

## Recipe format

```yaml
id: E-pod-kill-smf            # unique; also the file name
level: E                      # catalogue level (E, D, later A/B)
class: pod_kill               # fault class, shared by recipes that differ only in target/magnitude
title: SMF process crash      # one line, human-readable
description: >                # what it simulates and why it is interesting
  ...
target:
  nf: smf                     # pod label app=<nf> in namespace f5g
schedule:
  onset_s: 60                 # seconds after the workload starts
  duration_s: 0               # how long the fault is held (0 = one-shot, e.g. a kill)
  observe_s: 120              # how long to wait for the target to recover
  recovery_log: <regex>       # optional: log line of the (new) pod that proves it works again
magnitude: {}                 # class-specific knobs (latency_ms, loss_pct, cpu_workers, ...)
expected:                     # the hypothesis we label with (checked, not assumed, in M4)
  first_affected: smf
  procedures: [pdu_session_establishment, pdu_session_release]
inject:
  chaos:                      # a Chaos Mesh object; selector and namespace are filled in
    kind: PodChaos
    spec: {action: pod-kill, mode: one}
```

Injector kinds: `chaos` (Chaos Mesh: PodChaos, NetworkChaos, StressChaos, ...). Level-D
injectors (database, NRF, certificates) will add their own kinds.

## Labels (`runs/<id>/labels.json`, schema `f5g-labels/1`)

| field | meaning |
|---|---|
| `run_id`, `workload` | run and its workload window (`start_ns`, `end_ns`) |
| `fault` | `null` for a fault-free run, otherwise the fields below |
| `fault.id`, `level`, `class`, `title` | copied from the recipe |
| `fault.target` | `nf`, and the `pods` (name, uid, node) it hit |
| `fault.magnitude`, `fault.schedule` | as declared in the recipe |
| `fault.injected_ns` | when the injector applied the fault (host clock, ns) |
| `fault.removed_ns` | when the injector withdrew it (= `injected_ns` for one-shot faults) |
| `fault.recovered_ns` | when the target was observed working again (pod Ready and, if the recipe sets `recovery_log`, that log line seen), or `null` |
| `fault.expected` | copied from the recipe |
| `fault.injector` | how it was done (Chaos Mesh kind, object name, controller events) |

All timestamps are Unix nanoseconds on the host clock, the same clock as the workload records
and the telemetry, so the labels join with every modality on time.

## Known behaviour

`E-pod-kill-amf` and `E-pod-kill-upf` cause an outage that lasts until the end of the run: the
UERANSIM gNBs do not redo NG Setup after an AMF restart, and the lost GTP-U tunnels are not
re-created after a UPF restart. This is a property of the simulated RAN and is kept on purpose
(docs/M2-findings.md).

