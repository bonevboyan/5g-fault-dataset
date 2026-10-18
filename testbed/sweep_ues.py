#!/usr/bin/env python3
"""UE-ceiling sweep: for each N, clean `tb run` + resource sampling -> runs/sweep-<ts>/summary.json.

  ./sweep_ues.py 100 200 400 800 --duration 150

Per N it records bring-up time and gate outcome, workload KPIs (ping loss/RTT, procedure
success and latency), peak CPU/memory per container, and free5GC ERRO counts. It stops early
once a size fails its gates.
"""
import argparse, json, os, re, subprocess, sys, threading, time

ROOT = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(ROOT, "..", "runs")
NFS = ["nrf", "amf", "ausf", "udm", "udr", "pcf", "nssf", "smf", "chf", "upf"]


def mem_mib(s):
    v, unit = re.match(r"([\d.]+)\s*([KMG]i?B)", s).groups()
    return float(v) * {"KiB": 1 / 1024, "MiB": 1, "GiB": 1024, "KB": 1 / 1000, "MB": 1, "GB": 1000}[unit]


def sample(stop, out):
    while not stop.is_set():
        r = subprocess.run(["docker", "stats", "--no-stream", "--format",
                            "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.PIDs}}"],
                           capture_output=True, text=True)
        for line in r.stdout.splitlines():
            name, cpu, mem, pids = line.split("\t")
            d = out.setdefault(name, {"cpu_max": 0.0, "cpu_sum": 0.0, "n": 0, "mem_max_mib": 0.0,
                                      "pids_max": 0})
            c = float(cpu.rstrip("%") or 0)
            d["cpu_max"] = max(d["cpu_max"], c)
            d["cpu_sum"] += c
            d["n"] += 1
            d["mem_max_mib"] = max(d["mem_max_mib"], mem_mib(mem.split("/")[0].strip()))
            d["pids_max"] = max(d["pids_max"], int(pids or 0))
        stop.wait(5)


def rows(path):
    return [json.loads(l) for l in open(path)] if os.path.exists(path) else []


def pct(xs, q):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))], 1) if xs else None


def measure(n, a, sweep_dir):
    run_id = f"{os.path.basename(sweep_dir)}-n{n}"
    stats, stop = {}, threading.Event()
    th = threading.Thread(target=sample, args=(stop, stats), daemon=True)
    th.start()
    t0 = time.time()
    rc = subprocess.run([os.path.join(ROOT, "tb"), "run", "--ues", str(n), "--duration", str(a.duration),
                         "--seed", str(a.seed), "--tempo", str(a.tempo), "--run-id", run_id, "--keep"]).returncode
    stop.set()
    th.join()
    run_dir = os.path.join(RUNS, run_id)
    m = json.load(open(os.path.join(run_dir, "manifest.json")))
    errs = {}
    for nf in NFS:
        r = subprocess.run(["docker", "logs", nf], capture_output=True, text=True)
        errs[nf] = (r.stdout + r.stderr).count("[ERRO]")
    kpi = rows(os.path.join(run_dir, "workload", "kpi.jsonl"))
    rtt = [r["rtt_ms"] for r in kpi if r["ok"]]
    procs = {}
    for r in rows(os.path.join(run_dir, "workload", "procedures.jsonl")):
        if r.get("outcome") == "skipped":
            continue
        p = procs.setdefault(r["procedure"], {"n": 0, "ok": 0, "aborted": 0, "t3580": 0, "lat": []})
        p["n"] += 1
        p["ok"] += r["outcome"] == "ok"
        p["aborted"] += r["outcome"] == "aborted"
        p["t3580"] += bool(r.get("t3580_retry"))
        if r["outcome"] == "ok":
            p["lat"].append(r["latency_ms"])
    for p in procs.values():
        lat = p.pop("lat")
        p["p50_ms"], p["p95_ms"] = pct(lat, .5), pct(lat, .95)
    ready = {g["gate"]: g.get("seconds") for g in m["gates"]}
    pop = rows(os.path.join(run_dir, "workload", "population.jsonl"))
    res = {
        "ues": n, "phase": m["phase"], "error": m.get("error"), "wall_s": round(time.time() - t0),
        "bringup_s": round((_ts(m["ready_at"]) - _ts(m["started_at"])), 1),
        "ue_gate_s": next((v for k, v in ready.items() if "UEs registered" in k), None),
        "pings": len(kpi), "ping_loss_pct": round(100 * (1 - len(rtt) / len(kpi)), 3) if kpi else None,
        "rtt_p50_ms": pct(rtt, .5), "rtt_p95_ms": pct(rtt, .95), "rtt_p99_ms": pct(rtt, .99),
        "ping_rounds": len({r["ts_ns"] for r in kpi}),
        "min_registered": min((p["registered"] for p in pop), default=None),
        "min_active_sessions": min((p["active_sessions"] for p in pop), default=None),
        "procedures": procs, "erro_lines": errs,
        "resources": {k: {"cpu_avg": round(v["cpu_sum"] / max(v["n"], 1), 1), "cpu_max": v["cpu_max"],
                          "mem_max_mib": round(v["mem_max_mib"]), "pids_max": v["pids_max"]}
                      for k, v in stats.items() if not k.startswith(("k3d-", "moral-", "f5g-store"))},
    }
    subprocess.run([os.path.join(ROOT, "tb"), "down"])
    return rc, res


def _ts(iso):
    from datetime import datetime
    return datetime.fromisoformat(iso).timestamp()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("sizes", type=int, nargs="+")
    p.add_argument("--duration", type=float, default=150)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--tempo", type=int, default=100, help="ms between UE start-ups")
    a = p.parse_args()
    sweep_dir = os.path.join(RUNS, time.strftime("sweep-%Y%m%dT%H%M"))
    os.makedirs(sweep_dir, exist_ok=True)
    results = []
    for n in a.sizes:
        print(f"\n######## N = {n}", flush=True)
        rc, res = measure(n, a, sweep_dir)
        results.append(res)
        with open(os.path.join(sweep_dir, "summary.json"), "w") as f:
            json.dump({"args": vars(a), "results": results}, f, indent=2)
        print(json.dumps({k: res[k] for k in ("ues", "phase", "bringup_s", "ping_loss_pct",
                                              "rtt_p50_ms", "rtt_p99_ms", "erro_lines")}), flush=True)
        if rc != 0 or res["phase"] != "done":
            print(f"stopping: N={n} ended in phase {res['phase']}")
            break
    print(f"\nsummary: {os.path.relpath(sweep_dir)}/summary.json")


if __name__ == "__main__":
    main()
