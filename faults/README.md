# Fault library

One YAML file per fault ("recipe"). `tb run --fault faults/<level>/<name>.yaml` injects it during
the workload and writes the ground-truth labels to `runs/<id>/labels.json`. A run without
`--fault` writes the same file with `"fault": null`, so fault-free runs are labelled as normal.

Levels follow `docs/5g_fault_catalogue.pdf`. v1: **E** (operational baseline, Chaos Mesh), **D**
(dependency and data layer) and the registration/paging half of **A** (signalling storms). A's
packet-level faults and level **B**/**C** wait for more capacity (see `docs/M2-findings.md`).

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

Injector kinds (exactly one per recipe):

- `chaos`: a Chaos Mesh object (PodChaos, NetworkChaos, StressChaos, ...). Level E, plus the
  level-D network faults on the database (`target.nf: db`).
- `steps`: `apply` actions, and the `revert` actions that undo them after `duration_s`. Level D:

  ```yaml
  inject:
    steps:
      apply:  [{mongo: "<js>"}]     # run in the testbed MongoDB (database free5gc; also the NRF registry)
      revert: [{mongo: "<js>"}]     # convention: apply saves what it changes in f5g_fault_backup
  ```

  `cert: expired | untrusted` gives the target NF a key/cert its peers must reject (expired
  yesterday but signed by the testbed CA, or valid but signed by an unknown CA) and restarts its
  pod; `cert: restore` removes it and restarts again. Mechanism: an optional per-NF secret
  `f5g-pki-override-<nf>` that the pod copies over its testbed cert at start-up
  (`testbed/k8s/render.py`). The revert always runs, also when the run ends early or a step fails.

  Level A (signalling storms) uses three more `steps` actions, each with a `_stop` partner that is
  given the same spec (a YAML anchor) so apply and revert agree on what to undo:

  ```yaml
  inject:
    steps:
      apply:  [{storm: &s {count: 300, mode: auth_reject}}]   # or paging_storm
      revert: [{storm_stop: *s}]
  ```

  - `storm`: start `count` extra UERANSIM UEs across the running gNB pods. `mode: auth_reject`
    uses IMSIs with no subscriber record (every registration fails authentication at the UDM);
    `mode: valid` provisions the subscribers first (webconsole REST API) so they register and set
    up PDU sessions. `storm_stop` kills the UEs and, for a valid storm, deletes the subscribers.
  - `paging_storm`: a `valid` burst that, once registered, is repeatedly released to CM-IDLE and
    sent downlink data, so the core must page it — flooding the paging / service-request path
    (AMF paging, SMF N1N2MessageTransfer, UPF buffering) rather than registration. `paging_storm_stop`
    ends the loop, kills the UEs and deletes the subscribers.

  Storm UEs take IMSI indices from `index_base` (default 900000), a range the workload's UEs never
  reach, so they are matched by a distinct IMSI prefix and never collide with legitimate UEs.

For a `steps` fault, `target.nf` is where the root cause lives (`db` for data faults, `nrf` for
registry faults, the NF itself for certificate faults, `amf` for the signalling storms), not
necessarily the pod the commands run in.

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
| `fault.injector` | how it was done: Chaos Mesh object and controller events, or the steps and a log of each step's time and output |

All timestamps are Unix nanoseconds on the host clock, the same clock as the workload records
and the telemetry, so the labels join with every modality on time.

## Known behaviour

`E-pod-kill-amf` and `E-pod-kill-upf` cause an outage that lasts until the end of the run: the
UERANSIM gNBs do not redo NG Setup after an AMF restart, and the lost GTP-U tunnels are not
re-created after a UPF restart. This is a property of the simulated RAN and is kept on purpose
(docs/M2-findings.md).

UEs whose registration fails during `D-db-auth-corrupt` or `D-nrf-drop-udm` stay deregistered
for the rest of the run (UERANSIM does not retry after a reject). Also a RAN property.
