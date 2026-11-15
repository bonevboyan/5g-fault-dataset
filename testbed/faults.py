"""Fault injection for `tb run --fault <recipe.yaml>` and the ground-truth labels of a run.

A recipe (format: faults/README.md) is injected on a timer relative to the workload start, in a
background thread, while the workload driver runs. The thread records what it did and when, and
`write_labels` turns that into runs/<id>/labels.json (schema f5g-labels/1).
"""
import json
import os
import re
import subprocess
import threading
import time

import yaml

NS = "f5g"
SCHEMA = "f5g-labels/1"
CHAOS_API = "chaos-mesh.org/v1alpha1"
REQUIRED = ("id", "level", "class", "title", "target", "schedule", "inject")


def load(path):
    with open(path) as f:
        r = yaml.safe_load(f)
    missing = [k for k in REQUIRED if k not in r]
    if missing:
        raise ValueError(f"{path}: recipe is missing {', '.join(missing)}")
    if os.path.splitext(os.path.basename(path))[0] != r["id"]:
        raise ValueError(f"{path}: file name must equal the recipe id ({r['id']})")
    if set(r["inject"]) != {"chaos"}:
        raise ValueError(f"{path}: unknown injector {sorted(r['inject'])} (supported: chaos)")
    r.setdefault("magnitude", {})
    r.setdefault("expected", {})
    r["schedule"].setdefault("duration_s", 0)
    r["schedule"].setdefault("observe_s", 120)
    return r


def _kc(*args, input=None, check=True):
    p = subprocess.run(["kubectl", *args], input=input, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args[:3])}: {p.stderr.strip()[:300]}")
    return p.stdout


def _pods(nf):
    """[(name, uid, node, ready)] of the target's pods."""
    out = json.loads(_kc("get", "pods", "-n", NS, "-l", f"app={nf}", "-o", "json"))
    res = []
    for p in out["items"]:
        ready = any(c["type"] == "Ready" and c["status"] == "True" for c in p["status"].get("conditions", []))
        if p["metadata"].get("deletionTimestamp"):
            ready = False
        res.append((p["metadata"]["name"], p["metadata"]["uid"], p["spec"].get("nodeName"), ready))
    return res


def chaos_object(recipe, run_id):
    c = recipe["inject"]["chaos"]
    name = f"{recipe['id'].lower()}-{run_id}".replace("_", "-")[:63].rstrip("-")
    spec = dict(c["spec"])
    spec["selector"] = {"namespaces": [NS], "labelSelectors": {"app": recipe["target"]["nf"]}}
    dur = recipe["schedule"]["duration_s"]
    if dur:   # Chaos Mesh also withdraws it by itself, in case `tb` dies before it deletes the object
        spec.setdefault("duration", f"{dur}s")
    return {"apiVersion": CHAOS_API, "kind": c["kind"],
            "metadata": {"name": name, "namespace": NS, "labels": {"f5g/run": run_id, "f5g/fault": recipe["id"]}},
            "spec": spec}


class Injection(threading.Thread):
    """Waits until `t0 + onset_s`, injects, holds or observes, withdraws; never raises."""

    def __init__(self, recipe, run_id, t0_ns, log):
        super().__init__(daemon=True)
        self.r, self.run_id, self.t0_ns, self.log = recipe, run_id, t0_ns, log
        self.obj = chaos_object(recipe, run_id)
        self.rec = {"injected_ns": None, "removed_ns": None, "recovered_ns": None, "pods": [], "error": None,
                    "events": []}
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def run(self):
        s = self.r["schedule"]
        nf = self.r["target"]["nf"]
        try:
            if self._cancel.wait(max(0.0, self.t0_ns / 1e9 + s["onset_s"] - time.time())):
                self.rec["error"] = "cancelled before onset (workload ended early)"
                return
            before = _pods(nf)
            self.rec["pods"] = [{"name": n, "uid": u, "node": node} for n, u, node, _ in before]
            _kc("apply", "-f", "-", input=json.dumps(self.obj))
            self.rec["injected_ns"] = time.time_ns()
            self.log(f"fault {self.r['id']}: injected ({self.obj['kind']} {self.obj['metadata']['name']})")
            self._await_events(10)
            if s["duration_s"]:
                self._cancel.wait(s["duration_s"])
                self._delete()
                self.rec["removed_ns"] = time.time_ns()
                self.log(f"fault {self.r['id']}: removed after {s['duration_s']}s")
            else:
                self.rec["removed_ns"] = self.rec["injected_ns"]
            self._await_recovery(nf, {u for _, u, _, _ in before}, s["observe_s"])
        except Exception as e:  # noqa: BLE001 — a failed injection is recorded, the run goes on
            self.rec["error"] = str(e)
            self.log(f"fault {self.r['id']}: FAILED: {e}")
        finally:
            self._delete()

    def _await_recovery(self, nf, old_uids, timeout):
        """Recovered = a Ready target pod exists again (after a kill: a new pod) and it is working."""
        one_shot_kill = self.obj["kind"] == "PodChaos" and self.obj["spec"].get("action") == "pod-kill"
        deadline = time.time() + timeout
        while time.time() < deadline and not self._cancel.is_set():
            pods = _pods(nf)
            fresh = [n for n, uid, _, ready in pods if ready and (not one_shot_kill or uid not in old_uids)]
            if fresh and self._working(fresh[0]):
                self.rec["recovered_ns"] = time.time_ns()
                self.rec["pods_after"] = [{"name": n, "uid": u, "node": node} for n, u, node, _ in pods]
                self.log(f"fault {self.r['id']}: target Ready again after "
                         f"{(self.rec['recovered_ns'] - self.rec['removed_ns']) / 1e9:.1f}s")
                return
            time.sleep(0.5)
        self.log(f"fault {self.r['id']}: target not recovered within {timeout}s")

    def _working(self, pod):
        """The recipe's `recovery_log` regex (e.g. the SMF's PFCP re-association) has appeared in
        the pod's log; without one, pod Ready is taken as recovered."""
        pattern = self.r["schedule"].get("recovery_log")
        if not pattern:
            return True
        out = _kc("logs", "-n", NS, pod, "--tail=-1", check=False)
        return re.search(pattern, out) is not None

    def _await_events(self, timeout):
        """Chaos Mesh's records exist only while the fault is active (it clears them on recovery),
        so read them right after injection; a fault with no record is flagged."""
        deadline = time.time() + timeout
        while time.time() < deadline and not self.rec["events"]:
            self._collect_events()
            if not self.rec["events"]:
                time.sleep(0.5)
        if not self.rec["events"]:
            self.log(f"fault {self.r['id']}: no Chaos Mesh record of the injection within {timeout}s")

    def _collect_events(self):
        """Chaos Mesh's own record of what it did to which container (best effort)."""
        out = _kc("get", self.obj["kind"].lower(), self.obj["metadata"]["name"], "-n", NS, "-o", "json", check=False)
        try:
            st = json.loads(out).get("status", {})
        except ValueError:
            return
        events = [{"target": c.get("id"), **ev}
                  for c in st.get("experiment", {}).get("containerRecords", []) or []
                  for ev in c.get("events", []) or []]
        if events:
            self.rec["events"] = events

    def _delete(self):
        _kc("delete", self.obj["kind"].lower(), self.obj["metadata"]["name"], "-n", NS,
            "--ignore-not-found", "--wait=false", check=False)


def write_labels(run_dir, run_id, workload_start_ns, workload_end_ns, recipe=None, injection=None):
    fault = None
    if recipe is not None:
        rec = injection.rec if injection else {}
        fault = {k: recipe[k] for k in ("id", "level", "class", "title", "magnitude", "schedule", "expected")}
        fault["target"] = {"nf": recipe["target"]["nf"], "pods": rec.get("pods", []),
                           "pods_after": rec.get("pods_after", [])}
        fault.update({k: rec.get(k) for k in ("injected_ns", "removed_ns", "recovered_ns")})
        fault["injector"] = {"kind": "chaos-mesh", "object": injection.obj if injection else None,
                             "events": rec.get("events", []), "error": rec.get("error")}
    labels = {"schema": SCHEMA, "run_id": run_id,
              "workload": {"start_ns": workload_start_ns, "end_ns": workload_end_ns}, "fault": fault}
    with open(os.path.join(run_dir, "labels.json"), "w") as f:
        json.dump(labels, f, indent=2)
    return labels
