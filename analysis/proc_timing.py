#!/usr/bin/env python3
"""Log-timed procedure latencies for a finished run.

The workload driver times each procedure by polling UE state (a `kubectl exec` + 2 `nr-cli` calls
at 4 Hz), so its latencies carry ~0.25-0.4 s of observation lag plus the kubectl round trip of the
command that started the procedure. This script re-times every record in
`runs/<id>/workload/procedures.jsonl` from the log lines the UE (UERANSIM, ms timestamps) and the
AMF/SMF (ns timestamps) write at each protocol step, and writes

    runs/<id>/workload/procedures_timed.jsonl   every driver record + a `log` object:
        start_ns, end_ns, latency_ms   log-derived procedure boundaries (null if not found)
        start_event, end_event         which log line gave each boundary (see EVENTS)
        + per-procedure extras (t3580_retries, duplicate_psi, paging_to_sr_ms, gap_ms, ...)
    runs/<id>/logs/{ueransim,amf,smf}.jsonl.gz   the log lines used (so re-timing is offline)

All timestamps are the writer's own clock. Every pod runs on one host (one kernel clock), so UE
and NF timestamps are directly comparable; on real multi-node hardware this needs PTP/NTP.

The driver's poll-based `latency_ms` is kept unchanged: it is what an external observer saw, and
it bounds the log-timed value from above.

    analysis/proc_timing.py runs/<id>             # fetch from Loki (or the cache), time, summarize
    analysis/proc_timing.py runs/<id> --summary   # only summarize procedures_timed.jsonl
"""
import argparse, bisect, datetime as dt, gzip, json, os, re, sys, time, urllib.parse, urllib.request

# ---------------------------------------------------------------- log sources
# (service, LogQL line filter). UE lines are IMSI-prefixed by ue.start(); NF lines are kept only
# if they can be tied to a UE (SUPI, AMF-UE-NGAP-ID or an IMSI in the text).
SOURCES = {
    "ueransim": r'|~ "^\\[imsi-"',
    "amf": r'!= "[GIN]" |~ "imsi-|AU:[0-9]"',
    "smf": r'|~ "supi:imsi-"',
}

UE_LINE = re.compile(r"^\[(imsi-\d+)\] \[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3})\] \[(\w+)\] \[(\w+)\] (.*)$")
NF_LINE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+)Z \[(\w+)\]\[\w+\](.*)$")
AU = re.compile(r"AU:(\d+)\(3GPP\)")
SUPI = re.compile(r"(?:SUPI:|supi:|UE\[|AmfUe\[)(imsi-\d+)")

# event name -> (source, regex on the message). Groups named `psi` / `sd` are kept as attributes.
# `debug` events exist only when UE NAS debug lines are kept (ue.py allowlist, since 2026-09-28);
# older runs fall back to the info-level or core-side events noted per procedure below.
EVENTS = {
    # UE (UERANSIM)
    "ue_reg_initiated":  ("ueransim", r"UE switches to state \[MM-REGISTER-INITIATED\]"),
    "ue_reg_ok":         ("ueransim", r"Initial Registration is successful"),
    "ue_dereg_initiated": ("ueransim", r"UE switches to state \[MM-DEREGISTER-INITIATED\]"),
    "ue_pdu_req":        ("ueransim", r"Sending PDU Session Establishment Request"),           # debug
    "ue_pdu_ok":         ("ueransim", r"PDU Session establishment is successful PSI\[(?P<psi>\d+)\]"),
    "ue_t3580":          ("ueransim", r"Retransmitting PDU Session Establishment Request due to T3580"),
    "ue_pdu_rel_req":    ("ueransim", r"Sending PDU Session Release Request for PSI\[(?P<psi>\d+)\]"),  # debug
    "ue_pdu_rel_done":   ("ueransim", r"Performing local release of PDU session\[(?P<psi>\d+)\]"),     # debug
    "ue_cm_idle":        ("ueransim", r"UE switches to state \[CM-IDLE\]"),
    "ue_sr_initiated":   ("ueransim", r"UE switches to state \[MM-SERVICE-REQUEST-INITIATED\]"),
    "ue_sr_ok":          ("ueransim", r"Service Accept received"),
    # AMF
    "amf_dereg_req":     ("amf", r"Handle Deregistration Request"),
    "amf_ue_removed":    ("amf", r"AmfUe\[imsi-\d+\] is removed"),
    "amf_an_rel_req":    ("amf", r"\] Ue Context in GMM-Registered"),   # gNB's UE Context Release Request
    "amf_paging":        ("amf", r"Send Paging"),
    "amf_select_smf":    ("amf", r"Select SMF \[snssai: \{Sst:\d+ Sd:(?P<sd>\w+)\}"),
    "amf_dup_psi":       ("amf", r"Duplicated PDU session ID\[(?P<psi>\d+)\]"),
    "amf_smctx_release": ("amf", r"Release SmContext\[(?P<psi>\d+)\]"),
    # a re-establishment request that races the release: the AMF drops the old context itself
    "amf_smctx_local_release": ("amf", r"Local release duplicated SmContext\[(?P<psi>\d+)\]"),
    # SMF
    "smf_release_ip":    ("smf", r"\[pdu_session_id:(?P<psi>\d+)\].*\] Release IP\["),
}
_EV = {k: (src, re.compile(rx)) for k, (src, rx) in EVENTS.items()}


# ---------------------------------------------------------------- fetch (Loki) + cache
def loki_fetch(store, run_id, service, flt, start_ns, end_ns):
    """All lines of one (run, service) in [start, end], oldest first, paginated past Loki's limit."""
    # the M1 compose testbed labels NF containers free5gc-<nf>
    q = f'{{run_id="{run_id}",service_name=~"{service}|free5gc-{service}"}} {flt}'
    out, seen, cur = [], set(), start_ns
    while True:
        url = f"http://{store}:3100/loki/api/v1/query_range?" + urllib.parse.urlencode(
            {"query": q, "start": cur, "end": end_ns, "limit": 5000, "direction": "forward"})
        with urllib.request.urlopen(url, timeout=60) as r:
            res = json.load(r)["data"]["result"]
        page = sorted((int(t), l) for s in res for t, l in s["values"])
        new = [(t, l) for t, l in page if (t, l) not in seen]
        out += new
        seen.update(new)
        if len(page) < 5000 or not new:
            return out
        cur = page[-1][0]   # inclusive: lines sharing the last timestamp are de-duplicated via `seen`


def load_lines(run_dir, store, settle):
    """{service: [(loki_ts_ns, line)]}, from runs/<id>/logs/ if cached, else from Loki."""
    logs = os.path.join(run_dir, "logs")
    cached = {s: os.path.join(logs, f"{s}.jsonl.gz") for s in SOURCES}
    if all(os.path.exists(p) for p in cached.values()):
        return {s: [tuple(json.loads(x)) for x in gzip.open(p, "rt")] for s, p in cached.items()}
    run_id = os.path.basename(os.path.normpath(run_dir))
    with open(os.path.join(run_dir, "workload", "workload.json")) as f:
        w = json.load(f)
    # from the start of bring-up: the AMF binds AMF-UE-NGAP-IDs to SUPIs at registration, and a
    # UE registered during bring-up is only identified by that id on its later N2 messages
    t0 = w["started"]["ts_ns"] - 10 * 10**9
    try:
        with open(os.path.join(run_dir, "manifest.json")) as f:
            t0 = min(t0, _ns(json.load(f)["started_at"][:23]) - 10 * 10**9)
    except (OSError, KeyError, ValueError):
        pass
    t1 = (w.get("stopped") or {}).get("ts_ns", time.time_ns()) + 30 * 10**9
    # the log pipeline (filelog -> batch -> Loki) lags a few seconds behind; right after a run,
    # wait until the line counts stop growing
    prev, lines = None, {}
    for _ in range(max(1, settle // 3)):
        lines = {s: loki_fetch(store, run_id, s, flt, t0, t1) for s, flt in SOURCES.items()}
        n = tuple(len(v) for v in lines.values())
        if n == prev:
            break
        prev = n
        if settle:
            time.sleep(3)
    if not lines.get("ueransim") or not lines.get("amf"):
        # never cache an empty fetch: it would hide the logs if they arrive (or are fixed) later
        raise RuntimeError(f"no UE/AMF log lines in Loki for run {run_id} "
                           f"({ {s: len(v) for s, v in lines.items()} }): is the log pipeline exporting?")
    os.makedirs(logs, exist_ok=True)
    for s, v in lines.items():
        with gzip.open(cached[s], "wt") as f:
            for rec in v:
                f.write(json.dumps(rec) + "\n")
    return lines


# ---------------------------------------------------------------- parse -> per-UE event timelines
def _ns(iso):
    """'2026-09-27 17:11:23.217' or '2026-09-27T17:11:23.217792081' (UTC) -> epoch ns."""
    iso = iso.replace("T", " ")
    whole, _, frac = iso.partition(".")
    base = dt.datetime.strptime(whole, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    return int(base.timestamp()) * 10**9 + int((frac + "000000000")[:9])


def timelines(lines):
    """{imsi: {event: [(ts_ns, attrs)]}} sorted by time. AMF lines that carry only an
    AMF-UE-NGAP-ID (before the SUPI is known, or on N2 messages) are attributed via the
    AU -> SUPI binding seen on the AMF's other lines for the same id."""
    ev = {}

    def add(u, name, ts, m):
        ev.setdefault(u, {}).setdefault(name, []).append((ts, {k: v for k, v in m.groupdict().items() if v}))

    by_src = {}
    for name, (src, rx) in _EV.items():
        by_src.setdefault(src, []).append((name, rx))
    for _, line in lines.get("ueransim", []):
        m = UE_LINE.match(line)
        if not m:
            continue
        u, ts, msg = m.group(1), _ns(m.group(2)), m.group(5)
        for name, rx in by_src["ueransim"]:
            mm = rx.search(msg)
            if mm:
                add(u, name, ts, mm)
    for src in ("amf", "smf"):
        parsed = []
        au2supi = {}
        for _, line in lines.get(src, []):
            m = NF_LINE.match(line)
            if not m:
                continue
            ts, rest = _ns(m.group(1)), m.group(3)
            a, s = AU.search(rest), SUPI.search(rest)
            if a and s:
                au2supi[a.group(1)] = s.group(1)   # AMF-UE-NGAP-IDs are allocated once, never reused
            parsed.append((ts, rest, a.group(1) if a else None, s.group(1) if s else None))
        for ts, rest, a, s in parsed:
            u = s or au2supi.get(a)
            if not u:
                continue
            for name, rx in by_src[src]:
                mm = rx.search(rest)
                if mm:
                    add(u, name, ts, mm)
    for per in ev.values():
        for v in per.values():
            v.sort(key=lambda x: x[0])
    return ev


class UE:
    def __init__(self, ev):
        self.ev = ev
        self.keys = {k: [t for t, _ in v] for k, v in ev.items()}

    def first(self, name, after, before=None, **attrs):
        """(ts, attrs) of the first `name` event in [after, before] whose attrs match, else None."""
        v = self.ev.get(name, [])
        for i in range(bisect.bisect_left(self.keys.get(name, []), after), len(v)):
            t, at = v[i]
            if before is not None and t > before:
                return None
            if all(at.get(k) == str(x) for k, x in attrs.items()):
                return t, at
        return None

    def count(self, name, after, before):
        k = self.keys.get(name, [])
        return bisect.bisect_right(k, before) - bisect.bisect_left(k, after)


# ---------------------------------------------------------------- per-procedure timing rules
MS = 10**6
PRE = 100 * MS      # log lines may be stamped slightly before the driver's own start stamp
POST = 2000 * MS    # the poll that observed completion can come well after the log line


def _res(s, e, s_ev, e_ev, **extra):
    ok = s is not None and e is not None and e[0] >= s[0]
    return {"start_ns": s[0] if s else None, "end_ns": e[0] if e else None,
            "latency_ms": (e[0] - s[0]) / MS if ok else None,
            "start_event": s_ev if s else None, "end_event": e_ev if e else None, **extra}


def _first_of(ue, names, after, before, **attrs):
    """First match among fallback event names (tried in order) -> (hit, name)."""
    for n in names:
        h = ue.first(n, after, before, **attrs)
        if h:
            return h, n
    return None, None


def time_record(r, ue, ctx):
    """Log timing for one driver record. `ctx` carries the previous log-timed record of the same
    UE (a registration follows its switch-off; a re-establishment follows its release)."""
    p = r["procedure"]
    lo, hi = r["ts_ns"] - PRE, r.get("end_ns", r["ts_ns"]) + POST
    if p == "deregistration_switch_off":
        s = ue.first("ue_dereg_initiated", lo, hi)
        e = ue.first("amf_ue_removed", s[0] if s else lo, hi)
        return _res(s, e, "ue_dereg_initiated", "amf_ue_removed")
    if p == "initial_registration":
        s = ue.first("ue_reg_initiated", lo, hi)
        e = ue.first("ue_reg_ok", s[0] if s else lo, hi)
        return _res(s, e, "ue_reg_initiated", "ue_reg_ok")
    if p == "pdu_session_establishment_initial":
        prev = ctx.get("initial_registration")
        s = (prev["end_ns"], {}) if prev and prev.get("end_ns") else None
        oks = [ue.first("ue_pdu_ok", s[0] if s else lo, hi, psi=psi) for psi in (1, 2)]
        e = max(oks, key=lambda x: x[0]) if all(oks) else None
        return _res(s, e, "ue_reg_ok", "ue_pdu_ok(all)",
                    t3580_retries=ue.count("ue_t3580", s[0], hi) if s else None)
    if p == "pdu_session_release":
        psi = r.get("psi")
        s, s_ev = _first_of(ue, ["ue_pdu_rel_req", "smf_release_ip"], lo, hi, psi=psi)
        e, e_ev = _first_of(ue, ["ue_pdu_rel_done", "amf_smctx_release", "amf_smctx_local_release"], s[0] if s else lo, hi, psi=psi)
        return _res(s, e, s_ev, e_ev)
    if p == "pdu_session_establishment":
        # UERANSIM re-establishes on its own, and the request can reach the core before the
        # release has finished there (that is the T3580 race), so the search starts at the
        # log-timed release *start*
        rel = ctx.get("pdu_session_release") or {}
        after = rel.get("start_ns") or lo - POST
        psi = rel.get("psi")
        e = ue.first("ue_pdu_ok", after, hi, **({"psi": psi} if psi else {}))
        end = e[0] if e else hi
        # start: the UE's request (debug line). Else the AMF's first sight of it: normally it is
        # forwarded to an SMF (Select SMF); on the race the AMF drops it as a duplicate PSI and
        # only the T3580 retransmission ~16 s later is forwarded.
        s, s_ev = _first_of(ue, ["ue_pdu_req"], after, end)
        if not s:
            cands = [(ue.first("amf_dup_psi", after, end, **({"psi": psi} if psi else {})), "amf_dup_psi"),
                     (ue.first("amf_select_smf", after, end, sd=r.get("slice_sd")), "amf_select_smf")]
            s, s_ev = min(((h, n) for h, n in cands if h), key=lambda x: x[0][0], default=(None, None))
        return _res(s, e, s_ev, "ue_pdu_ok",
                    t3580_retries=ue.count("ue_t3580", after, end),
                    duplicate_psi=ue.count("amf_dup_psi", after, end),
                    # the UE's pause between the core finishing the release and its new request
                    # (undefined on the race: there the new request itself ends the release)
                    gap_ms=(s[0] - rel["end_ns"]) / MS if s and rel.get("end_ns") and s_ev != "amf_dup_psi" else None)
    if p == "an_release":
        s = ue.first("amf_an_rel_req", lo, hi)
        e = ue.first("ue_cm_idle", s[0] if s else lo, hi)
        return _res(s, e, "amf_an_rel_req", "ue_cm_idle")
    if p == "service_request_mo":
        s = ue.first("ue_sr_initiated", lo, hi)
        e = ue.first("ue_sr_ok", s[0] if s else lo, hi)
        return _res(s, e, "ue_sr_initiated", "ue_sr_ok")
    if p == "service_request_mt_paging":
        s = ue.first("amf_paging", lo, hi)
        sr = ue.first("ue_sr_initiated", s[0] if s else lo, hi)
        e = ue.first("ue_sr_ok", sr[0] if sr else lo, hi)
        return _res(s, e, "amf_paging", "ue_sr_ok",
                    paging_to_sr_ms=(sr[0] - s[0]) / MS if s and sr else None,
                    sr_ms=(e[0] - sr[0]) / MS if sr and e else None)
    return None


def retime(run_dir, store="127.0.0.1", settle=30):
    lines = load_lines(run_dir, store, settle)
    ev = timelines(lines)
    src = os.path.join(run_dir, "workload", "procedures.jsonl")
    recs = [json.loads(l) for l in open(src)]
    recs.sort(key=lambda r: r["ts_ns"])
    ctx = {}   # ue -> {procedure: last log result}
    out = []
    for r in recs:
        if r.get("outcome") in ("skipped",):
            out.append(r)
            continue
        c = ctx.setdefault(r["ue"], {})
        t = time_record(r, UE(ev.get(r["ue"], {})), c)
        if t is not None:
            if r["procedure"] == "pdu_session_release":
                t["psi"] = r.get("psi")
            c[r["procedure"]] = t
            r = {**r, "log": t}
        out.append(r)
    dst = os.path.join(run_dir, "workload", "procedures_timed.jsonl")
    with open(dst, "w") as f:
        for r in out:
            f.write(json.dumps(r) + "\n")
    return dst


# ---------------------------------------------------------------- summary
def _p(v, q):
    return v[min(len(v) - 1, int(q * len(v)))] if v else float("nan")


def summarize(run_dir):
    rows = [json.loads(l) for l in open(os.path.join(run_dir, "workload", "procedures_timed.jsonl"))]
    by = {}
    for r in rows:
        if r.get("outcome") == "ok":
            by.setdefault(r["procedure"], []).append(r)
    print(f"{'procedure':<34} {'n':>4} {'timed':>5}  {'poll p50':>9} {'log p50':>8} {'log p90':>8}  events")
    for p, rs in sorted(by.items()):
        t = [r for r in rs if (r.get("log") or {}).get("latency_ms") is not None]
        poll = sorted(r["latency_ms"] for r in rs)
        lg = sorted(r["log"]["latency_ms"] for r in t)
        evs = sorted({f"{r['log']['start_event']}->{r['log']['end_event']}" for r in t})
        print(f"{p:<34} {len(rs):>4} {len(t):>5}  {_p(poll, .5):>7.0f}ms {_p(lg, .5):>6.0f}ms {_p(lg, .9):>6.0f}ms  "
              + ", ".join(evs))
    est = [r for r in by.get("pdu_session_establishment", []) if r.get("log")]
    if est:
        drv = sum(bool(r.get("t3580_retry")) for r in est)
        lg = sum(bool(r["log"].get("t3580_retries")) for r in est)
        dup = sum(bool(r["log"].get("duplicate_psi")) for r in est)
        gaps = sorted(r["log"]["gap_ms"] for r in est if r["log"].get("gap_ms") is not None)
        print(f"pdu re-establishment: T3580 retries {lg}/{len(est)} (driver heuristic: {drv}), "
              f"AMF duplicate-PSI drops {dup}, release-end->request gap p50 {_p(gaps, .5):.0f} ms")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("run_dir")
    p.add_argument("--store", default=os.environ.get("STORE_HOST", "127.0.0.1"))
    p.add_argument("--settle", type=int, default=0,
                   help="s to wait for the log pipeline to catch up (use ~30 right after a run)")
    p.add_argument("--summary", action="store_true")
    a = p.parse_args()
    if not a.summary:
        print(f"wrote {retime(a.run_dir, a.store, a.settle)}", file=sys.stderr)
    summarize(a.run_dir)


if __name__ == "__main__":
    main()
