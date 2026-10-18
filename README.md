# 5g-fault-dataset

A multimodal anomaly-detection / root-cause-analysis dataset for a cloud-native 5G core
(free5GC v4.2.3 on Kubernetes), with logs, metrics, end-to-end distributed traces, Kubernetes
events and UE-plane KPIs joined by `trace_id`/`run_id`, plus RCA ground-truth labels per
injected fault. The dataset is the paper's contribution; the testbed here produces it.

- New here? Read [ONBOARDING.md](ONBOARDING.md): what exists, why it looks this way, how to
  set it up, and what to do next.
- Design and roadmap: [docs/PLAN.md](docs/PLAN.md). Milestone write-ups with evidence:
  [M0](docs/M0-findings.md) (tracing), [M1](docs/M1-findings.md) (stable testbed),
  [M1b](docs/M1b-findings.md) (k8s, TLS, idle mode, multi-gNB, end-to-end traces).
- Testbed usage: [testbed/README.md](testbed/README.md).

Status (2026-10-18): M0, M1, M1b done. M2 (fault library) not started; a procedure-timeout
regression introduced on 2026-09-28 must be bisected first (see ONBOARDING.md).
