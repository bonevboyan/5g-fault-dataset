#!/usr/bin/env python3
"""N-UE background workload for a READY testbed run.

Four concurrent, seeded activities against the UEs started by `tb up`:
  user plane   every --ping-interval s, one ICMP echo through a rotating window of
               --ping-sample active PDU sessions (per-UE / per-slice RTT and loss) -> kpi.jsonl
  registration every --reg-interval s, switch one UE off and restart it: full initial
               registration (AUSF/UDM/UDR auth chain, AMF context setup) + PDU sessions
  pdu session  every --pdu-interval s, release one UE's slice-2 session; the UE re-establishes it
               (SMF/PCF/UPF PFCP, CHF charging)
  idle mode    every --idle-interval s, release one UE to CM-IDLE (gNB UE context release); after a
               seeded dwell it is woken by uplink data (Service Request) or by downlink data from
               the DN (UPF buffering -> SMF N1N2 -> AMF paging -> Service Request)
                                                                               -> procedures.jsonl
and a periodic population snapshot (#registered, #active sessions)             -> population.jsonl

Every record carries a UTC timestamp (ISO + epoch ns) so it joins with traces/logs/metrics.
Churn slots (time offset + victim UE) come from per-activity seeded RNGs on an absolute time
grid, so runs with the same --seed perturb the same UEs at the same offsets; a slot that can't
run (victim still busy with an earlier slot) is logged as `skipped`, never re-drawn.
"""
import argparse, datetime as dt, json, os, random, re, sys, threading, time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ue  # noqa: E402

# At most 4 probes in flight per gNB pod: UERANSIM's gNB forwards GTP in one user-space loop, so a
# burst of simultaneous echoes measures the probe's own queueing (p50 3x higher at ~21 in flight).
PING_SCRIPT = r"""
printf '%s\n' {ifaces} | xargs -P 4 -I{{}} sh -c \
  'r=$(ping -c 1 -W {timeout} -I "$1" {target} 2>/dev/null | grep -o "time=[0-9.]*" | cut -d= -f2); echo "$1 ${{r:-x}}"' _ {{}}
"""


def stamp():
    t = time.time_ns()
    return {"ts": dt.datetime.fromtimestamp(t / 1e9, dt.timezone.utc).isoformat(timespec="milliseconds"),
            "ts_ns": t}


class Sink:
    """Thread-safe JSONL appender."""
    def __init__(self, path):
        self.f = open(path, "a", buffering=1)
        self.lock = threading.Lock()

    def write(self, rec):
        with self.lock:
            self.f.write(json.dumps(rec) + "\n")


class Driver:
    def __init__(self, a):
        self.a = a
        self.stop = threading.Event()
        self.busy = set()             # UEs currently inside a churn procedure (skip their pings)
        self.busy_lock = threading.Lock()
        self.slot_threads = []
        self.last_states = {}         # refreshed by snapshot(); ping_round reads it
        self.ngap = ue.NgapIndex()    # IMSI -> AMF-UE-NGAP-ID, for gNB-side UE release
        # the population snapshot runs ~2 nr-cli calls per UE on the RAN node, whose CPU also does
        # the gNB's user-space GTP forwarding; ping rounds never overlap it, so the tooling's own
        # load doesn't show up as RTT (measured: 2-3x p50 inflation when they overlapped)
        self.ran_poll = threading.Lock()
        # UE -> addresses probed since its last idle wake-up. A Service Request only re-activates
        # the user plane of the session with pending data; the UE's other session stays
        # deactivated until its own first packet (which is lost while that re-activation runs).
        self.woke = {}
        self.ping_cursor = 0
        self.ues = [ue.imsi(i) for i in range(a.ues)]
        os.makedirs(a.out, exist_ok=True)
        self.kpi = Sink(os.path.join(a.out, "kpi.jsonl"))
        self.proc = Sink(os.path.join(a.out, "procedures.jsonl"))
        self.pop = Sink(os.path.join(a.out, "population.jsonl"))

    # ------------------------------------------------------------- helpers
    def claim(self, u):
        with self.busy_lock:
            if u in self.busy:
                return False
            self.busy.add(u)
            return True

    def release(self, u):
        with self.busy_lock:
            self.busy.discard(u)

    def wait_state(self, u, pred, timeout):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout and not self.stop.is_set():
            s = ue.state(u)
            if s is not None and pred(s):
                return s
            time.sleep(0.25)   # each poll is a kubectl exec + 2 nr-cli calls on the RAN node
        return None

    def outcome(self, s):
        """'aborted' = run ended mid-procedure (not a failure of the core)."""
        return "ok" if s else ("aborted" if self.stop.is_set() else "timeout")

    def every(self, interval, fn):
        """Run fn every `interval` s until stopped (unseeded sampling activities)."""
        while not self.stop.wait(interval):
            try:
                fn()
            except Exception as e:  # noqa: BLE001 — one failed iteration must not kill the load
                print(f"[{fn.__name__}] {e}", file=sys.stderr)

    def scheduled(self, name, interval, fn):
        """Run fn(victim) on a fixed, seeded schedule of (time offset, victim) slots.

        Slot k's time and victim depend only on (--seed, name, k), never on how long earlier
        procedures took, so two runs with the same seed perturb the same UEs at the same
        offsets. A slot whose victim is still busy is recorded as skipped, not re-drawn."""
        rng = random.Random(f"{self.a.seed}:{name}")
        at = 0.0
        k = 0
        while True:
            at += interval * (1 + rng.uniform(-self.a.jitter, self.a.jitter))
            victim = rng.choice(self.ues)
            delay = self.t0 + at - time.monotonic()
            if delay > 0 and self.stop.wait(delay):
                return
            if self.stop.is_set():
                return
            base = {**stamp(), "ue": victim, "procedure": name, "slot": k, "slot_offset_s": round(at, 3)}
            k += 1
            if not self.claim(victim):
                self.proc.write({**base, "outcome": "skipped", "reason": "ue busy"})
                continue
            # each slot runs in its own thread: a procedure stuck on a T3580 retry must not
            # shift the slots after it
            t = threading.Thread(target=self._slot, args=(name, fn, victim, k - 1), daemon=True)
            self.slot_threads.append(t)
            t.start()

    def _slot(self, name, fn, victim, slot):
        try:
            fn(victim, slot)
        except Exception as e:  # noqa: BLE001
            print(f"[{name}] {e}", file=sys.stderr)
        finally:
            self.release(victim)

    # ------------------------------------------------------------- activities
    def ping_round(self):
        """Ping a rotating window of --ping-sample active sessions (all of them if 0).

        The session list comes from the last population snapshot, not a fresh per-UE query,
        so the cost per round stays flat as N grows; sessions of UEs mid-churn are skipped."""
        addr2if = ue.tun_by_address()          # {ue_ip: (gnb, ifname)}
        live = sorted((*addr2if[x["address"]], u, x["sd"], x["address"])
                      for u, s in self.last_states.items() if u not in self.busy and s.get("cm") != "CM-IDLE"
                      for x in s["sessions"] if x["state"] == "PS-ACTIVE" and x["address"] in addr2if)
        k = self.a.ping_sample
        if k and len(live) > k:
            start = self.ping_cursor % len(live)
            live = (live + live)[start:start + k]
            self.ping_cursor += k
        by_gnb = {}
        for g, ifc, u, sd, addr in live:
            by_gnb.setdefault(g, {})[ifc] = (u, sd, addr)
        if not by_gnb:
            return
        target = self.a.target or ue.dn_ip()
        self.ran_poll.acquire()
        try:
            self._ping(by_gnb, target)
        finally:
            self.ran_poll.release()

    def _ping(self, by_gnb, target):

        def one(g):
            return g, ue.dexec("sh", "-c", PING_SCRIPT.format(ifaces=" ".join(by_gnb[g]), target=target,
                                                               timeout=self.a.ping_timeout), gnb=g)
        with ThreadPoolExecutor(max_workers=len(by_gnb)) as ex:
            results = list(ex.map(one, by_gnb))
        now = stamp()
        for g, out in results:
            for line in out.splitlines():
                ifc, _, rtt = line.partition(" ")
                if ifc not in by_gnb[g]:
                    continue
                u, sd, addr = by_gnb[g][ifc]
                ok = rtt != "x"
                # perturbed: a churn/idle slot claimed this UE while the probe was in flight, so a
                # loss here is the workload's own procedure, not a network condition
                seen = self.woke.get(u)
                first_after_wake = seen is not None and addr not in seen
                if seen is not None:
                    seen.add(addr)
                self.kpi.write({**now, "ue": u, "gnb": g, "slice_sd": sd, "ue_ip": addr, "ifname": ifc,
                                "target": target, "ok": ok, "rtt_ms": float(rtt) if ok else None,
                                "perturbed": u in self.busy, "first_after_wake": first_after_wake})

    def reg_churn(self, u, slot=None):
        t0 = stamp()
        ue.stop(u)
        gone = ue.state(u) is None
        t1 = stamp()
        self.proc.write({"ts": t0["ts"], "ts_ns": t0["ts_ns"], "end_ns": t1["ts_ns"], "ue": u,
                         "procedure": "deregistration_switch_off",
                         "outcome": "ok" if gone else "unknown",
                         "latency_ms": (t1["ts_ns"] - t0["ts_ns"]) / 1e6})
        t0 = stamp()
        ue.start(u)
        s = self.wait_state(u, lambda s: s["rm"] == "RM-REGISTERED", self.a.proc_timeout)
        t1 = stamp()
        self.proc.write({"ts": t0["ts"], "ts_ns": t0["ts_ns"], "end_ns": t1["ts_ns"], "ue": u,
                         "procedure": "initial_registration",
                         "outcome": self.outcome(s),
                         "latency_ms": (t1["ts_ns"] - t0["ts_ns"]) / 1e6,
                         "mm_state": (s or ue.state(u) or {}).get("mm")})
        if s:
            want = len(ue.SLICES)
            s = self.wait_state(u, lambda s: s["pdu_sessions"] >= want, self.a.proc_timeout)
            t2 = stamp()
            self.proc.write({"ts": t1["ts"], "ts_ns": t1["ts_ns"], "end_ns": t2["ts_ns"], "ue": u,
                             "procedure": "pdu_session_establishment_initial",
                             "outcome": self.outcome(s),
                             "latency_ms": (t2["ts_ns"] - t1["ts_ns"]) / 1e6,
                             "active_sessions": (s or ue.state(u) or {}).get("pdu_sessions")})

    def pdu_churn(self, u, slot=None):
        s = ue.state(u)
        sd = ue.SLICES[-1][1]
        sess = next((x for x in (s or {}).get("sessions", [])
                     if x["sd"] == sd and x["state"] == "PS-ACTIVE"), None)
        if sess is None:
            return
        t0 = stamp()
        ue.cli(u, f"ps-release {sess['id']}")
        old = (sess["id"], sess["address"])
        # released = the original (PSI, address) is gone; UERANSIM may already have re-created
        # the session under the same PSI with a new address by the time we look
        s = self.wait_state(u, lambda s: all((x["id"], x["address"]) != old or x["state"] != "PS-ACTIVE"
                                             for x in s["sessions"]), self.a.proc_timeout)
        t1 = stamp()
        self.proc.write({"ts": t0["ts"], "ts_ns": t0["ts_ns"], "end_ns": t1["ts_ns"], "ue": u,
                         "procedure": "pdu_session_release", "slice_sd": sd, "psi": old[0],
                         "outcome": self.outcome(s),
                         "latency_ms": (t1["ts_ns"] - t0["ts_ns"]) / 1e6})
        if not s:
            return
        # UERANSIM re-establishes a uecfg-configured session on its own, ~60-400 ms after the
        # release; we only observe it. (An extra ps-establish collides with it: UAC "barred",
        # duplicate PSI.) When the request lands while the AMF is still tearing down the old
        # SmContext, free5GC drops it and the UE recovers on T3580 (~16 s): t3580_retry=True.
        active = lambda s: any(x["sd"] == sd and x["state"] == "PS-ACTIVE" for x in s["sessions"])
        s = self.wait_state(u, active, self.a.proc_timeout)
        t2 = stamp()
        lat = (t2["ts_ns"] - t1["ts_ns"]) / 1e6
        self.proc.write({"ts": t1["ts"], "ts_ns": t1["ts_ns"], "end_ns": t2["ts_ns"], "ue": u,
                         "procedure": "pdu_session_establishment", "slice_sd": sd,
                         "trigger": "ue-auto", "outcome": self.outcome(s), "latency_ms": lat,
                         "t3580_retry": bool(s) and lat > self.a.t3580 * 1000 * 0.8})

    def idle_cycle(self, u, slot):
        """CM-CONNECTED -> (AN release) CM-IDLE -> dwell -> wake by uplink data (Service Request,
        mobile-originated) or downlink data from the DN (UPF buffering, SMF N1N2, AMF paging,
        mobile-terminated). Dwell and wake mode come from the slot's own seeded RNG."""
        rng = random.Random(f"{self.a.seed}:idle:{slot}")
        dwell = rng.uniform(*self.a.idle_dwell)
        mode = "mt" if rng.random() < self.a.idle_mt_share else "mo"
        s = ue.state(u)
        sess = next((x for x in (s or {}).get("sessions", []) if x["state"] == "PS-ACTIVE" and x["address"]), None)
        if not s or s["cm"] != "CM-CONNECTED" or sess is None:
            return
        t0 = stamp()
        resolved = ue.release_to_idle(u, self.ngap)
        s = self.wait_state(u, lambda s: s["cm"] == "CM-IDLE", 5) if resolved else None
        t1 = stamp()
        self.proc.write({"ts": t0["ts"], "ts_ns": t0["ts_ns"], "end_ns": t1["ts_ns"], "ue": u, "slot": slot,
                         "procedure": "an_release", "outcome": self.outcome(s) if resolved else "unresolved",
                         "latency_ms": (t1["ts_ns"] - t0["ts_ns"]) / 1e6})
        if not s:
            return
        if self.stop.wait(dwell):
            return
        t0 = stamp()
        if mode == "mo":
            tun = ue.tun_by_address().get(sess["address"])
            data = threading.Thread(target=lambda: ue.dexec("ping", "-c", "2", "-W", "2", "-I", tun[1],
                                                            self.a.target or ue.dn_ip(), gnb=tun[0]) if tun else None)
        else:
            data = threading.Thread(target=lambda: ue.dn_ping(sess["address"]))
        data.start()
        s = self.wait_state(u, lambda s: s["cm"] == "CM-CONNECTED", self.a.proc_timeout)
        t1 = stamp()
        data.join()
        self.woke[u] = set()
        self.proc.write({"ts": t0["ts"], "ts_ns": t0["ts_ns"], "end_ns": t1["ts_ns"], "ue": u, "slot": slot,
                         "procedure": "service_request_mt_paging" if mode == "mt" else "service_request_mo",
                         "idle_dwell_s": round(dwell, 3), "outcome": self.outcome(s),
                         "latency_ms": (t1["ts_ns"] - t0["ts_ns"]) / 1e6})

    def snapshot(self):
        with self.ran_poll:
            st = ue.states()
        self.last_states = st
        self.pop.write({**stamp(), "ues_expected": len(self.ues), "ues_up": len(st),
                        "registered": sum(s["rm"] == "RM-REGISTERED" for s in st.values()),
                        "idle": sum(s.get("cm") == "CM-IDLE" for s in st.values()),
                        "active_sessions": sum(s["pdu_sessions"] for s in st.values())})

    # ------------------------------------------------------------- main
    def run(self):
        a = self.a
        with open(os.path.join(a.out, "workload.json"), "w") as f:
            json.dump({**vars(a), "started": stamp()}, f, indent=2)
        self.t0 = time.monotonic()
        self.snapshot()
        acts = [(self.every, (a.snapshot_interval, self.snapshot))]
        if a.ping_interval > 0:
            acts.append((self.every, (a.ping_interval, self.ping_round)))
        if a.reg_interval > 0:
            acts.append((self.scheduled, ("registration_churn", a.reg_interval, self.reg_churn)))
        if a.pdu_interval > 0:
            acts.append((self.scheduled, ("pdu_session_churn", a.pdu_interval, self.pdu_churn)))
        if a.idle_interval > 0:
            acts.append((self.scheduled, ("idle_cycle", a.idle_interval, self.idle_cycle)))
        threads = [threading.Thread(target=f, args=x, daemon=True) for f, x in acts]
        for t in threads:
            t.start()
        print(f"workload: {len(self.ues)} UEs, {a.duration}s -> {a.out}", flush=True)
        try:
            self.stop.wait(a.duration if a.duration > 0 else None)
        except KeyboardInterrupt:
            pass
        self.stop.set()
        for t in threads + self.slot_threads:
            t.join(timeout=a.proc_timeout + 5)
        with open(os.path.join(a.out, "workload.json")) as f:
            meta = json.load(f)
        meta["stopped"] = stamp()
        with open(os.path.join(a.out, "workload.json"), "w") as f:
            json.dump(meta, f, indent=2)


def summarize(out):
    """One-line-per-metric summary of a finished workload directory."""
    def rows(name):
        p = os.path.join(out, name)
        return [json.loads(l) for l in open(p)] if os.path.exists(p) else []
    allk = [r for r in rows("kpi.jsonl") if not r.get("perturbed")]
    kpi = [r for r in allk if not r.get("first_after_wake")]
    ok = [r["rtt_ms"] for r in kpi if r["ok"]]
    faw = [r for r in allk if r.get("first_after_wake")]
    print(f"pings: {len(kpi)}  loss: {100 * (1 - len(ok) / max(len(kpi), 1)):.2f}%  "
          f"rtt p50: {sorted(ok)[len(ok) // 2] if ok else float('nan'):.2f} ms   "
          f"(+{len(faw)} first-after-wake probes, {sum(not r['ok'] for r in faw)} lost: UP re-activation)")
    by, skipped = {}, {}
    for r in rows("procedures.jsonl"):
        if r.get("outcome") == "skipped":   # a scheduled slot whose victim was still busy
            skipped[r["procedure"]] = skipped.get(r["procedure"], 0) + 1
            continue
        by.setdefault(r["procedure"], []).append(r)
    if skipped:
        print("skipped slots (victim busy): " + ", ".join(f"{k}={v}" for k, v in sorted(skipped.items())))
    for p, rs in sorted(by.items()):
        lat = sorted(r["latency_ms"] for r in rs if r["outcome"] == "ok")
        ab = sum(r["outcome"] == "aborted" for r in rs)
        t3 = sum(bool(r.get("t3580_retry")) for r in rs)
        print(f"{p:<36} n={len(rs):<4} ok={len(lat):<4} aborted={ab:<2} t3580={t3:<2} "
              f"p50={lat[len(lat) // 2] if lat else float('nan'):.0f} ms")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", required=True, help="output dir, normally runs/<run_id>/workload")
    p.add_argument("--ues", type=int, required=True)
    p.add_argument("--duration", type=float, default=300, help="seconds; 0 = until Ctrl-C")
    p.add_argument("--target", default=None, help="DN host pinged through the UPF (default: auto, see ue.dn_ip)")
    p.add_argument("--ping-interval", type=float, default=2.0, help="0 disables user-plane pings")
    p.add_argument("--ping-timeout", type=int, default=1)
    p.add_argument("--ping-sample", type=int, default=64,
                   help="sessions pinged per round, rotating through all (0 = every session)")
    p.add_argument("--reg-interval", type=float, default=10.0, help="0 disables registration churn")
    p.add_argument("--pdu-interval", type=float, default=6.0, help="0 disables PDU-session churn")
    p.add_argument("--idle-interval", type=float, default=4.0, help="0 disables idle-mode cycles")
    p.add_argument("--idle-dwell", type=float, nargs=2, default=[5.0, 30.0], metavar=("MIN", "MAX"),
                   help="seconds a released UE stays CM-IDLE before its wake-up traffic")
    p.add_argument("--idle-mt-share", type=float, default=0.5,
                   help="fraction of wake-ups by downlink data (paging) vs uplink data")
    p.add_argument("--snapshot-interval", type=float, default=15.0)
    p.add_argument("--proc-timeout", type=float, default=25.0, help="> T3580 (16 s) retransmit")
    p.add_argument("--t3580", type=float, default=16.0, help="UE NAS T3580 (s), for retry flagging")
    p.add_argument("--jitter", type=float, default=0.3)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--summary", action="store_true", help="only summarize an existing --out dir")
    a = p.parse_args()
    if not a.summary:
        Driver(a).run()
    summarize(a.out)


if __name__ == "__main__":
    main()
