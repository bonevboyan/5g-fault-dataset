#!/usr/bin/env python3
"""Shape of the distributed traces of a run, from Tempo.

    analysis/trace_shape.py runs/<id> [--store HOST] [--limit N] [--json OUT]

Reads the run's manifest for the run id and time window, searches Tempo for traces tagged
run.id=<id> (up to --limit), fetches each one and reports: spans and services per trace, tree
depth, orphan spans, and which spans are roots. Used to validate end-to-end context propagation
(an AMF "ngap …" root with children in AUSF/UDM/UDR/NRF/PCF/SMF) against the per-hop baseline
(every root an "HTTP …" client span with exactly one server child).
"""
import argparse, collections, json, statistics, sys, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

def ts(s):
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())

def get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)

def search(host, run_id, start, end, limit):
    ids, seen = [], set()
    # Tempo caps one search; walk the window in slices so we can gather more than one page.
    step = max(30, (end - start) // 20)
    for t0 in range(start, end, step):
        q = urllib.parse.urlencode({"tags": f"run.id={run_id}", "limit": limit, "start": t0, "end": min(t0 + step, end)})
        for t in get(f"http://{host}:3200/api/search?{q}").get("traces", []):
            if t["traceID"] not in seen:
                seen.add(t["traceID"]); ids.append(t["traceID"])
        if len(ids) >= limit:
            break
    return ids[:limit]

KIND = {1: "internal", 2: "server", 3: "client", 4: "producer", 5: "consumer",
        "SPAN_KIND_INTERNAL": "internal", "SPAN_KIND_SERVER": "server", "SPAN_KIND_CLIENT": "client",
        "SPAN_KIND_PRODUCER": "producer", "SPAN_KIND_CONSUMER": "consumer"}

def attr(attrs, key):
    for a in attrs or []:
        if a.get("key") == key:
            v = a.get("value", {})
            return v.get("stringValue", v.get("intValue", v.get("boolValue")))
    return None

def shape(trace):
    spans = {}
    for b in trace.get("batches", []):
        svc = attr(b.get("resource", {}).get("attributes"), "service.name") or "?"
        for ss in b.get("scopeSpans", []):
            for s in ss.get("spans", []):
                spans[s["spanId"]] = {"svc": svc, "parent": s.get("parentSpanId") or "", "name": s["name"],
                                      "kind": KIND.get(s.get("kind"), str(s.get("kind"))),
                                      "nas": attr(s.get("attributes"), "nas.msg"), "supi": attr(s.get("attributes"), "supi")}
    roots = [s for s in spans.values() if not s["parent"]]
    orphans = [s for s in spans.values() if s["parent"] and s["parent"] not in spans]
    def depth(sid, d=0):
        kids = [k for k, v in spans.items() if v["parent"] == sid]
        return max([depth(k, d + 1) for k in kids], default=d)
    dmax = max([depth(k) for k, v in spans.items() if not v["parent"]], default=0)
    root = roots[0] if roots else (orphans[0] if orphans else None)
    rname = (root["name"] if root else "?")
    if rname.startswith("ngap ") and root.get("nas"):
        rname += " / " + root["nas"]
    return {"spans": len(spans), "services": sorted({s["svc"] for s in spans.values()}), "depth": dmax,
            "roots": len(roots), "orphans": len(orphans), "root_name": rname, "root_svc": root["svc"] if root else "?",
            "sbi_server": sum(1 for s in spans.values() if s["kind"] == "server"),
            "sbi_client": sum(1 for s in spans.values() if s["kind"] == "client" and s["name"].startswith("HTTP"))}

def pct(xs, p):
    xs = sorted(xs); return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else 0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run"); ap.add_argument("--store", default="127.0.0.1"); ap.add_argument("--limit", type=int, default=3000)
    ap.add_argument("--json"); ap.add_argument("--phase", choices=["all", "workload"], default="workload")
    a = ap.parse_args()
    m = json.load(open(f"{a.run}/manifest.json"))
    start = ts(m["workload_started_at"] if a.phase == "workload" and m.get("workload_started_at") else m["started_at"])
    end = ts(m.get("workload_ended_at") or m.get("ready_at") or m["started_at"]) + (0 if m.get("workload_ended_at") else 900)
    ids = search(a.store, m["run_id"], start - 5, end + 60, a.limit)
    print(f"run {m['run_id']}: {len(ids)} traces sampled ({a.phase} window {start}..{end})")
    with ThreadPoolExecutor(16) as ex:
        shapes = list(ex.map(lambda i: shape(get(f"http://{a.store}:3200/api/traces/{i}")), ids))
    if not shapes:
        sys.exit("no traces")
    n = len(shapes)
    spans = [s["spans"] for s in shapes]; svcs = [len(s["services"]) for s in shapes]; depth = [s["depth"] for s in shapes]
    print(f"\n| metric | value |\n|---|---|")
    print(f"| spans / trace p50 · p90 · max | {pct(spans,.5)} · {pct(spans,.9)} · {max(spans)} |")
    print(f"| services / trace p50 · p90 · max | {pct(svcs,.5)} · {pct(svcs,.9)} · {max(svcs)} |")
    print(f"| tree depth p50 · p90 · max | {pct(depth,.5)} · {pct(depth,.9)} · {max(depth)} |")
    print(f"| traces with ≥ 2 services | {sum(1 for x in svcs if x >= 2) / n:.1%} |")
    print(f"| traces with ≥ 3 services | {sum(1 for x in svcs if x >= 3) / n:.1%} |")
    print(f"| traces with exactly one root | {sum(1 for s in shapes if s['roots'] == 1) / n:.1%} |")
    print(f"| traces with orphan spans | {sum(1 for s in shapes if s['orphans']) / n:.1%} |")
    print(f"| root is an NGAP message (AMF) | {sum(1 for s in shapes if s['root_name'].startswith('ngap')) / n:.1%} |")
    print(f"| root is a PFCP message (SMF) | {sum(1 for s in shapes if s['root_name'].startswith('pfcp')) / n:.1%} |")
    print(f"| root is an SBI client span (per-hop only) | {sum(1 for s in shapes if s['root_name'].startswith('HTTP')) / n:.1%} |")
    by = collections.defaultdict(list)
    for s in shapes:
        by[(s["root_svc"], s["root_name"])].append(s)
    print(f"\n| root (service · span) | traces | spans p50 | services p50 | depth p50 | services seen |\n|---|---|---|---|---|---|")
    for (svc, name), ss in sorted(by.items(), key=lambda kv: -len(kv[1]))[:18]:
        seen = sorted({x for s in ss for x in s["services"]})
        print(f"| {svc} · {name} | {len(ss)} | {pct([s['spans'] for s in ss],.5)} | {pct([len(s['services']) for s in ss],.5)} | "
              f"{pct([s['depth'] for s in ss],.5)} | {' '.join(seen)} |")
    if a.json:
        json.dump({"run": m["run_id"], "n": n, "shapes": shapes}, open(a.json, "w"))

if __name__ == "__main__":
    main()
