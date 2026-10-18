#!/usr/bin/env python3
"""Observer-overhead A/B: compare two runs that differ only in in-NF tracing (tb run --no-tracing).

  analysis/ab_overhead.py runs/ab-trace-on runs/ab-trace-off
  analysis/ab_overhead.py runs/on-1 runs/on-2 -- runs/off-1 runs/off-2     (repetitions per arm)

Reports, per procedure, the median latency of each run with a bootstrap 95% CI and the
difference; the user-plane RTT; and each NF's mean CPU (cAdvisor, from Prometheus) over the
workload window. Latencies are the workload driver's observations (polling-based, so their
resolution is ~0.1-0.3 s on k8s); CPU is the more sensitive overhead signal.
"""
import json
import os
import random
import sys
import urllib.parse
import urllib.request
from datetime import datetime

PROM = os.environ.get("PROM", "http://127.0.0.1:9090")


def rows(path):
    return [json.loads(l) for l in open(path)] if os.path.exists(path) else []


def median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else float("nan")


def boot_ci(xs, n=2000, seed=0):
    if len(xs) < 3:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    meds = sorted(median([rng.choice(xs) for _ in xs]) for _ in range(n))
    return meds[int(0.025 * n)], meds[int(0.975 * n)]


def procs(run):
    by = {}
    for r in rows(os.path.join(run, "workload", "procedures.jsonl")):
        if r.get("outcome") == "ok" and "latency_ms" in r:
            by.setdefault(r["procedure"], []).append(r["latency_ms"])
    return by


def rtts(run):
    return [r["rtt_ms"] for r in rows(os.path.join(run, "workload", "kpi.jsonl"))
            if r["ok"] and not r.get("perturbed")]


def cpu(run):
    m = json.load(open(os.path.join(run, "manifest.json")))
    t0 = datetime.fromisoformat(m["workload_started_at"]).timestamp()
    t1 = datetime.fromisoformat(m["workload_ended_at"]).timestamp()
    rid = m["run_id"]
    q = (f'avg_over_time(sum by (container) (rate(container_cpu_usage_seconds_total{{run_id="{rid}",'
         f'namespace="f5g",container!="",container!="POD"}}[30s]))[{int(t1 - t0)}s:15s])')
    url = f"{PROM}/api/v1/query?" + urllib.parse.urlencode({"query": q, "time": t1})
    res = json.load(urllib.request.urlopen(url))["data"]["result"]
    return {r["metric"]["container"]: 100 * float(r["value"][1]) for r in res}


def pooled(runs, fn):
    out = {}
    for r in runs:
        for k, v in fn(r).items():
            out.setdefault(k, []).extend(v)
    return out


def main():
    args = sys.argv[1:]
    A, B = (args[:args.index("--")], args[args.index("--") + 1:]) if "--" in args else ([args[0]], [args[1]])
    pa, pb = pooled(A, procs), pooled(B, procs)
    print(f"A = {', '.join(map(os.path.basename, A))}   B = {', '.join(map(os.path.basename, B))}\n")
    print(f"{'procedure':<34} {'A median [95% CI]':>24} {'B median [95% CI]':>24} {'A-B':>8}")
    for p in sorted(set(pa) | set(pb)):
        xa, xb = pa.get(p, []), pb.get(p, [])
        ca, cb = boot_ci(xa), boot_ci(xb)
        print(f"{p:<34} {median(xa):7.0f} [{ca[0]:5.0f},{ca[1]:5.0f}] n={len(xa):<3}"
              f"{median(xb):7.0f} [{cb[0]:5.0f},{cb[1]:5.0f}] n={len(xb):<3} {median(xa) - median(xb):+7.0f}")
    ra, rb = sum((rtts(r) for r in A), []), sum((rtts(r) for r in B), [])
    print(f"\n{'user-plane RTT (ms)':<34} {median(ra):7.2f} [{boot_ci(ra)[0]:.2f},{boot_ci(ra)[1]:.2f}]"
          f"   {median(rb):7.2f} [{boot_ci(rb)[0]:.2f},{boot_ci(rb)[1]:.2f}]")
    try:
        cas, cbs = [cpu(r) for r in A], [cpu(r) for r in B]
        mean = lambda xs: sum(xs) / len(xs)
        spread = lambda xs: (max(xs) - min(xs)) / 2 if len(xs) > 1 else 0.0
        print(f"\n{'NF mean CPU (% of one core)':<34} {'A (+/- half-range)':>20} {'B (+/- half-range)':>20}")
        for c in sorted(set().union(*cas, *cbs)):
            xa, xb = [d.get(c, 0) for d in cas], [d.get(c, 0) for d in cbs]
            print(f"  {c:<32} {mean(xa):8.2f} +/-{spread(xa):5.2f}    {mean(xb):8.2f} +/-{spread(xb):5.2f}")
        core = [c for c in cas[0] if c not in ("ueransim", "mongo", "webui", "dn")]
        ta = [sum(d.get(c, 0) for c in core) for d in cas]
        tb = [sum(d.get(c, 0) for c in core) for d in cbs]
        print(f"  {'free5GC NFs total':<32} {mean(ta):8.2f} +/-{spread(ta):5.2f}    {mean(tb):8.2f} +/-{spread(tb):5.2f}"
              f"   per-run A={[round(x, 1) for x in ta]} B={[round(x, 1) for x in tb]}")
    except Exception as e:  # noqa: BLE001
        print(f"\n(CPU comparison unavailable: {e})")


if __name__ == "__main__":
    main()
