#!/usr/bin/env python3
"""Does a fault show up in every modality, at the time its labels say? (M2 validation)

    analysis/fault_signals.py runs/<id> [--store 127.0.0.1] [--bin 10] [--span 60]

Reads runs/<id>/labels.json and prints, per time bin around the injection (bin 0 starts at
`injected_ns`), one row per modality:
  logs     ERRO+WARN lines, all NFs (Loki)
  k8s      Kubernetes events about the target (Loki, service k8s-events)
  metrics  target container CPU (cores) and memory (MiB, newest pod) (Prometheus/cAdvisor)
  traces   error spans (Tempo)
  ue       failed procedures (timeout/aborted, by start time), latency p50 of successful
           procedures, ping loss and ping RTT p50 (workload records)
"""
import argparse, json, os, statistics, urllib.parse, urllib.request
from collections import Counter


def get(url, params):
    with urllib.request.urlopen(url + "?" + urllib.parse.urlencode(params), timeout=30) as r:
        return json.load(r)


def loki_lines(host, query, start_ns, end_ns):
    out, cursor = [], start_ns
    while True:
        res = get(f"http://{host}:3100/loki/api/v1/query_range",
                  {"query": query, "start": cursor, "end": end_ns, "limit": 5000, "direction": "forward"})
        batch = [(int(ts), line) for s in res["data"]["result"] for ts, line in s["values"]]
        out += batch
        if len(batch) < 5000:
            return out
        cursor = max(ts for ts, _ in batch) + 1


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("run_dir")
    p.add_argument("--store", default="127.0.0.1")
    p.add_argument("--bin", type=int, default=10, help="seconds per bin")
    p.add_argument("--span", type=int, default=60, help="seconds shown before and after the injection")
    a = p.parse_args()
    lab = json.load(open(os.path.join(a.run_dir, "labels.json")))
    f, run = lab["fault"], lab["run_id"]
    if not f or not f.get("injected_ns"):
        raise SystemExit("no injected fault in labels.json")
    t0, B = f["injected_ns"], a.bin * 10**9
    lo, hi = max(lab["workload"]["start_ns"], t0 - a.span * 10**9), min(lab["workload"]["end_ns"], t0 + a.span * 10**9)
    bins = list(range((lo - t0) // B, (hi - t0 - 1) // B + 1))   # only bins that start inside the workload
    b_of = lambda ns: (ns - t0) // B  # noqa: E731
    nf = f["target"]["nf"]
    rows = {}

    logs = loki_lines(a.store, f'{{run_id="{run}", service_name!~"k8s-events|ueransim|otelcol|node-exporter"}} |~ "\\\\[(ERRO|WARN)\\\\]"', lo, hi)
    rows["logs: ERRO+WARN"] = Counter(b_of(ts) for ts, _ in logs)
    ev = loki_lines(a.store, f'{{run_id="{run}", service_name="k8s-events"}} |= "{nf}-"', lo, hi)
    rows[f"k8s: events on {nf}"] = Counter(b_of(ts) for ts, _ in ev)

    res = get(f"http://{a.store}:9090/api/v1/query_range",
              {"query": f'sum(rate(container_cpu_usage_seconds_total{{run_id="{run}",container="{nf}"}}[1m]))',
               "start": lo / 1e9, "end": hi / 1e9, "step": a.bin})
    cpu = {}
    for s in res["data"]["result"]:
        for ts, v in s["values"]:
            cpu[b_of(int(float(ts) * 1e9))] = float(v)
    rows[f"metrics: {nf} CPU (cores)"] = cpu
    # memory per pod, newest pod wins: a restarted NF starts with empty state, so this drops
    res = get(f"http://{a.store}:9090/api/v1/query_range",
              {"query": f'max by (pod) (container_memory_working_set_bytes{{run_id="{run}",container="{nf}"}})',
               "start": lo / 1e9, "end": hi / 1e9, "step": a.bin})
    mem = {}
    for s in sorted(res["data"]["result"], key=lambda s: s["values"][0][0]):
        for ts, v in s["values"]:
            mem[b_of(int(float(ts) * 1e9))] = float(v) / 2**20
    rows[f"metrics: {nf} memory (MiB)"] = mem

    q = f'{{ resource.run.id = "{run}" && status = error }}'
    tr = get(f"http://{a.store}:3200/api/search", {"q": q, "start": lo // 10**9, "end": hi // 10**9 + 1,
                                                    "limit": 2000, "spss": 1})
    rows["traces: traces with error spans"] = Counter(b_of(int(t["startTimeUnixNano"])) for t in tr.get("traces", []))

    wl = os.path.join(a.run_dir, "workload")
    procs = [json.loads(x) for x in open(os.path.join(wl, "procedures.jsonl"))]
    rows["ue: failed procedures"] = Counter(b_of(r["ts_ns"]) for r in procs
                                            if r.get("outcome") in ("timeout", "aborted") and lo <= r["ts_ns"] <= hi)
    lat = {}
    for r in procs:
        if r.get("outcome") == "ok" and r.get("latency_ms") is not None and lo <= r["ts_ns"] <= hi:
            lat.setdefault(b_of(r["ts_ns"]), []).append(r["latency_ms"])
    rows["ue: procedure latency p50 (ms)"] = {b: float(statistics.median(v)) for b, v in lat.items()}
    pings = [json.loads(x) for x in open(os.path.join(wl, "kpi.jsonl"))]
    rtt = {}
    for r in pings:
        if r["ok"] and r.get("rtt_ms") is not None and lo <= r["ts_ns"] <= hi and not r.get("first_after_wake"):
            rtt.setdefault(b_of(r["ts_ns"]), []).append(r["rtt_ms"])
    tot, lost = Counter(), Counter()
    for r in pings:
        if lo <= r["ts_ns"] <= hi and not r.get("first_after_wake"):
            tot[b_of(r["ts_ns"])] += 1
            lost[b_of(r["ts_ns"])] += not r["ok"]
    rows["ue: ping loss %"] = {b: 100 * lost[b] / tot[b] for b in tot}
    rows["ue: ping RTT p50 (ms)"] = {b: float(statistics.median(v)) for b, v in rtt.items()}

    rec = (f["recovered_ns"] - t0) / 1e9 if f.get("recovered_ns") else None
    print(f"run {run}: fault {f['id']} on {nf}, injected at bin 0"
          + (f", target working again after {rec:.1f}s" if rec is not None else ", no recovery observed"))
    print(f"bins of {a.bin}s relative to the injection\n")
    w = max(len(k) for k in rows) + 2
    print("".ljust(w) + "".join(f"{b * a.bin:>+7}" for b in bins))
    for name, vals in rows.items():
        cells = []
        for b in bins:
            v = vals.get(b, 0)
            cells.append(f"{v:>7.2f}" if isinstance(v, float) and ("CPU" in name or "RTT" in name) else
                         f"{v:>7.1f}" if isinstance(v, float) else f"{v:>7}")
        print(name.ljust(w) + "".join(cells))


if __name__ == "__main__":
    main()
